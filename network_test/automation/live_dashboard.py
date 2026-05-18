"""
Local live dashboard for stream metrics.

The dashboard uses only the Python standard library. A tiny HTTP server serves
one HTML page and a JSON snapshot file that is updated by the stream test.
"""

from __future__ import annotations

import atexit
from datetime import datetime
import json
import math
import numbers
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import urlparse


def _safe_path_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "unknown"


def _sanitize_for_json(obj: Any) -> Any:
    """
    保证写入 metrics 的 JSON 可被浏览器 JSON.parse 解析。

    Python json 默认会把 float('nan') 等序列化成 NaN/Infinity，RFC 8259 不合法，
    前端整段解析失败后会表现为「页面不刷新」。
    """
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, numbers.Real):
        x = float(obj)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    return obj


def _persist_json_snapshot(path: Path, text: str) -> None:
    """
    尽力把快照写入磁盘（供事后查看）；失败时不抛错。

    Windows 上若浏览器/编辑器正打开目标 JSON，`Path.replace` 会 WinError 5；
    仪表盘改从内存字节提供 `/metrics.json`，不依赖本函数成功。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if path.exists():
                try:
                    path.unlink()
                    os.replace(tmp_path, path)
                    return
                except OSError:
                    pass
            else:
                os.replace(tmp_path, path)
                return
        # 仍失败则直接覆盖写一次（部分环境只锁 replace 不锁 write）
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
    except OSError:
        pass
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


# 与 HTML 中 <script src="/dashboard.js?v=..."> 一致；改 JS 时递增，避免浏览器缓存旧脚本。
_DASHBOARD_JS_VERSION = "4"


def _dashboard_js() -> bytes:
    """看板轮询逻辑（独立路由，便于在开发者工具 Network 里看到 dashboard.js / metrics.json）。"""
    return r"""
(function () {
  "use strict";
  var POLL_MS = 1000;
  var history = [];
  var maxPoints = 180;
  var refreshCount = 0;
  var pollOk = false;
  var CIRC = 327;

  function el(id) {
    return document.getElementById(id);
  }
  function nz(v, fallback) {
    return v === undefined || v === null ? fallback : v;
  }
  function fmt(v, digits) {
    digits = digits === undefined ? 3 : digits;
    return Number.isFinite(Number(v)) ? Number(v).toFixed(digits) : "-";
  }
  function setText(id, value) {
    var node = el(id);
    if (node) node.textContent = nz(value, "-");
  }
  function fmtElapsed(sec) {
    var s = Math.max(0, Math.floor(Number(sec) || 0));
    var h = String(Math.floor(s / 3600)).padStart(2, "0");
    var m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    var ss = String(s % 60).padStart(2, "0");
    return h + ":" + m + ":" + ss;
  }
  function setMetricColor(id, ok) {
    var node = el(id);
    if (!node) return;
    node.className = "metric-value " + (ok ? "c-green" : "c-red");
  }
  function setDim(barId, valId, pct) {
    var p = Math.max(0, Math.min(100, pct));
    var bar = el(barId);
    if (bar) bar.style.width = p + "%";
    setText(valId, Math.round(p) + "%");
  }
  function updateScore(m) {
    var loss = Number(m.loss_rate_percent || 0);
    var limit = Number(m.stream_loss_limit_percent_applied);
    if (!Number.isFinite(limit)) limit = 0.5;
    var parseErr = Number(m.parse_errors || 0);
    var frames = Number(m.frames_received || 0);
    var frameScore = frames > 0 ? Math.max(0, 100 - (parseErr / frames) * 100) : 50;
    var lossScore = Math.max(0, 100 - (loss / Math.max(limit, 0.01)) * 50);
    var parseScore = frames > 0 ? Math.max(0, 100 - parseErr * 5) : 80;
    var total = Math.round((frameScore + lossScore + parseScore) / 3);
    var arc = el("score-arc");
    if (arc) {
      arc.style.strokeDashoffset = String(CIRC * (1 - total / 100));
      arc.style.stroke = total >= 80 ? "#3fb950" : total >= 60 ? "#d29922" : "#f85149";
    }
    setText("score-num", String(total));
    setText(
      "score-grade",
      total >= 90 ? "优秀" : total >= 75 ? "良好" : total >= 60 ? "一般" : "需关注"
    );
    setDim("dim-frame", "dimv-frame", frameScore);
    setDim("dim-loss", "dimv-loss", lossScore);
    setDim("dim-parse", "dimv-parse", parseScore);
  }
  function pushHistory(m) {
    var wallS = Number(m.completed_scan_wall_interval_latest_s);
    var tsS = Number(m.scan_timestamp_interval_latest_s);
    history.push({
      hz: Number(m.implied_scan_rate_hz || 0),
      loss: Number(m.loss_rate_percent || 0),
      maxGapMs: Number(m.max_inter_frame_gap_s || 0) * 1000,
      wallIntervalMs: wallS > 0 ? wallS * 1000 : NaN,
      tsIntervalMs: tsS > 0 ? tsS * 1000 : NaN,
    });
    if (history.length > maxPoints) history.shift();
  }
  function niceStep(span) {
    if (!Number.isFinite(span) || span <= 0) return 1;
    var rough = span / 4;
    var mag = Math.pow(10, Math.floor(Math.log10(rough)));
    var norm = rough / mag;
    if (norm <= 1) return mag;
    if (norm <= 2) return 2 * mag;
    if (norm <= 5) return 5 * mag;
    return 10 * mag;
  }
  function formatAxisY(v, unit) {
    if (!Number.isFinite(v)) return "";
    if (unit === "%") return v.toFixed(2);
    if (unit === "Hz") return v.toFixed(1);
    if (unit === "ms") return v >= 1000 ? (v / 1000).toFixed(2) + "s" : v.toFixed(0);
    return v.toFixed(2);
  }
  /** 带 Y/X 刻度的折线图；series 为 [{key,color,label}, ...] */
  function drawChart(canvasId, series, yUnit) {
    var canvas = el(canvasId);
    if (!canvas || !series || !series.length) return;
    var ctx = canvas.getContext("2d");
    var w = canvas.width;
    var h = canvas.height;
    var padL = 52;
    var padR = 10;
    var padT = 20;
    var padB = 26;
    var plotW = w - padL - padR;
    var plotH = h - padT - padB;

    ctx.fillStyle = "#1a1f27";
    ctx.fillRect(0, 0, w, h);

    var allVals = [];
    series.forEach(function (s) {
      history.forEach(function (p) {
        var v = p[s.key];
        if (Number.isFinite(v)) allVals.push(v);
      });
    });
    var yMin = 0;
    var yMax = allVals.length ? Math.max.apply(null, allVals) : 1;
    if (yUnit === "%") yMax = Math.max(yMax, 0.01);
    else yMax = Math.max(yMax * 1.08, yMin + 0.001);
    var ySpan = yMax - yMin || 1;
    var yStep = niceStep(ySpan);
    var yTop = Math.ceil(yMax / yStep) * yStep;
    if (yTop <= yMin) yTop = yMin + yStep;

    ctx.strokeStyle = "#30363d";
    ctx.fillStyle = "#8b949e";
    ctx.font = "10px Consolas, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (var yv = yMin; yv <= yTop + yStep * 0.01; yv += yStep) {
      var yy = padT + plotH - ((yv - yMin) / (yTop - yMin || 1)) * plotH;
      ctx.beginPath();
      ctx.moveTo(padL, yy);
      ctx.lineTo(w - padR, yy);
      ctx.stroke();
      ctx.fillText(formatAxisY(yv, yUnit), padL - 6, yy);
    }
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    var n = history.length;
    var xLabels = n <= 1 ? [0] : [0, Math.floor((n - 1) / 2), n - 1];
    xLabels.forEach(function (idx) {
      var xx = padL + (n <= 1 ? 0 : (idx / (n - 1)) * plotW);
      var secAgo = (n - 1 - idx) * (POLL_MS / 1000);
      ctx.fillText("-" + secAgo + "s", xx, h - padB + 4);
    });
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";

    series.forEach(function (s) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      var started = false;
      history.forEach(function (p, i) {
        var v = p[s.key];
        if (!Number.isFinite(v)) return;
        var x = padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
        var y = padT + plotH - ((v - yMin) / (yTop - yMin || 1)) * plotH;
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      if (started) ctx.stroke();
    });

    var legX = padL;
    series.forEach(function (s, i) {
      ctx.fillStyle = s.color;
      ctx.fillRect(legX, 4, 10, 3);
      ctx.fillStyle = "#8b949e";
      ctx.font = "10px Consolas, monospace";
      ctx.fillText(s.label, legX + 14, 2);
      legX += ctx.measureText(s.label).width + 28;
    });
  }
  function drawSeries(canvasId, key, color, label, yUnit) {
    drawChart(canvasId, [{ key: key, color: color, label: label }], yUnit || "");
  }
  function updateDiagnosis(m, ok) {
    var badge = el("root-cause-badge");
    var text = el("diagnosis-text");
    if (!badge || !text) return;
    if (ok) {
      badge.textContent = "运行正常";
      badge.style.cssText = "background:#1a2e1a;color:#3fb950;border:1px solid #2a4a2a";
      text.textContent = "缺包率与扫描频率在阈值内，连续取数链路稳定。";
    } else {
      badge.textContent = "需要关注";
      badge.style.cssText = "background:#2e1a1a;color:#f85149;border:1px solid #4a2a2a";
      var loss = Number(m.loss_rate_percent || 0);
      var limit = Number(m.stream_loss_limit_percent_applied);
      if (!Number.isFinite(limit)) limit = 0.5;
      var parts = [];
      if (loss > limit) {
        parts.push("缺包率 " + fmt(loss, 4) + "% 超过阈值 " + fmt(limit, 4) + "%");
      }
      if (Number(m.parse_errors || 0) > 0) {
        parts.push("解析错误 " + m.parse_errors + " 次");
      }
      text.textContent = parts.length
        ? parts.join("\n")
        : "部分指标偏离期望，请结合原始抓包排查。";
    }
  }
  function refresh() {
    pollOk = false;
    var url = "/metrics.json?ts=" + Date.now();
    return fetch(url, {
      cache: "no-store",
      headers: { "Cache-Control": "no-cache", Accept: "application/json" },
    })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (payload) {
        var m = payload.metrics || {};
        var status = payload.status || "running";
        refreshCount += 1;
        var dot = el("dot");
        if (dot) {
          dot.className =
            "dot " +
            (status === "final" ? "done" : status === "waiting" ? "waiting" : "running");
        }
        setText(
          "statusText",
          status === "final"
            ? "采样结束"
            : status === "waiting"
              ? "等待 pytest 推数..."
              : "采集中"
        );
        setText("hdr-device", m.model || "-");
        setText("hdr-host", m.host || "-");
        setText("hdr-elapsed", fmtElapsed(m.window_duration_s));
        setText("hdr-refresh", "#" + refreshCount);
        var limit = Number(m.stream_loss_limit_percent_applied);
        if (!Number.isFinite(limit)) limit = 0.5;
        var ok = Number(m.loss_rate_percent || 0) <= limit;
        setText("updated", payload.updated_at || "-");
        setText("result", ok ? "PASS" : "CHECK");
        setText("expectedHz", fmt(m.expected_scan_rate_hz, 2));
        setText("expectedHzSub", fmt(m.expected_scan_rate_hz, 2));
        setText("lossLimit", fmt(limit, 4));
        setText(
          "rawCapture",
          m.raw_capture_truncated ? "truncated" : m.raw_capture_path ? "enabled" : "-"
        );
        setText("elapsed", fmt(m.window_duration_s, 2) + " s");
        setText("frames", nz(m.frames_received, "-"));
        setText("completeSub", nz(m.completed_scans, "-"));
        setText("scansSeen", nz(m.scans_seen, nz(m.completed_scans, "-")));
        setText("loss", fmt(m.loss_rate_percent, 4));
        setMetricColor("loss", ok);
        setText("missingSub", nz(m.missing_packets, "-"));
        setText("scanHz", fmt(m.implied_scan_rate_hz, 3));
        setText("maxGap", fmt(Number(m.max_inter_frame_gap_s || 0) * 1000, 2));
        setText("tsGap", m.scan_timestamp_interval_avg_display || "-");
        setText("wallGap", m.completed_scan_wall_interval_avg_display || "-");
        setText("wallGapSub", m.completed_scan_wall_interval_avg_display || "-");
        setText("parseErrors", nz(m.parse_errors, "-"));
        setText("duplicates", nz(m.duplicate_packets, "-"));
        setText("ignored", nz(m.boundary_partial_scans_ignored, "-"));
        setText("rawFrames", nz(m.raw_frames_captured, "-"));
        setText("rawPath", m.raw_capture_path || payload.dashboard_url || "-");
        updateScore(m);
        updateDiagnosis(m, ok);
        pushHistory(m);
        drawSeries("chartHz", "hz", "#58a6ff", "Hz", "Hz");
        drawSeries("chartLoss", "loss", "#f85149", "%", "%");
        drawSeries("chartGap", "maxGapMs", "#3fb950", "ms", "ms");
        drawChart(
          "chartScanInterval",
          [
            { key: "wallIntervalMs", color: "#bc8cff", label: "墙钟圈间隔" },
            { key: "tsIntervalMs", color: "#58a6ff", label: "时间戳圈间隔" },
          ],
          "ms"
        );
        pollOk = true;
        setText("fetch-error", "");
      })
      .catch(function (err) {
        var dot = el("dot");
        if (dot) dot.className = "dot waiting";
        setText(
          "statusText",
          "拉取 /metrics.json 失败（须 pytest 运行中且从本页 http://127.0.0.1:端口/ 打开）"
        );
        setText("fetch-error", "上次错误: " + (err && err.message ? err.message : String(err)));
      })
      .finally(function () {
        setText(
          "footer-time",
          new Date().toLocaleString() + (pollOk ? " · 已拉取 metrics.json" : " · 轮询失败")
        );
      });
  }
  function schedulePoll() {
    refresh().finally(function () {
      window.setTimeout(schedulePoll, POLL_MS);
    });
  }
  function boot() {
    console.info("[stream-dashboard] dashboard.js v4 loaded; polling /metrics.json every 1s");
    setText("diagnosis-text", "脚本已加载，每 1 秒请求 /metrics.json（请保持 pytest 连续取数在跑）。");
    schedulePoll();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
""".encode(
        "utf-8"
    )


def _dashboard_html() -> bytes:
    # 视觉风格对齐 通信测试/web_dashboard.html：深色终端风、卡片顶栏渐变、等宽字体。
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store, max-age=0" />
  <title>LiDAR 连续取数实时监控</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0d1117; --surface: #161b22; --border: #30363d;
      --text: #e6edf3; --subtext: #8b949e;
      --green: #3fb950; --yellow: #d29922; --red: #f85149;
      --cyan: #58a6ff; --magenta: #bc8cff;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg); color: var(--text);
      font-family: 'Cascadia Code', Consolas, 'Courier New', monospace;
      font-size: 13px; min-height: 100vh;
    }
    header {
      background: linear-gradient(135deg, #1a2233 0%, #0d1b2a 100%);
      border-bottom: 1px solid var(--border);
      padding: 12px 24px; display: flex; align-items: center;
      justify-content: space-between; position: sticky; top: 0; z-index: 100;
    }
    .header-left { display: flex; align-items: center; gap: 16px; }
    .logo { font-size: 18px; font-weight: bold; color: var(--cyan); letter-spacing: 1px; }
    .logo span { color: var(--green); }
    .version-badge {
      background: #1f3a5f; color: var(--cyan); border: 1px solid #2d5f8a;
      border-radius: 4px; padding: 2px 8px; font-size: 11px;
    }
    .header-right {
      display: flex; align-items: center; gap: 18px; font-size: 12px; color: var(--subtext);
    }
    .header-right .hl { color: var(--text); }
    #conn-status { display: flex; align-items: center; gap: 6px; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--subtext); }
    .dot.running { background: var(--green); animation: pulse 2s infinite; }
    .dot.done { background: var(--cyan); }
    .dot.waiting { background: var(--yellow); }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
    main { padding: 20px 24px; display: grid; gap: 16px; max-width: 1440px; margin: 0 auto; }
    .health-row { display: grid; grid-template-columns: 300px 1fr; gap: 16px; }
    .card {
      background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
      padding: 16px; position: relative; overflow: hidden;
    }
    .card::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
      background: linear-gradient(90deg, var(--cyan), var(--magenta));
    }
    .card-title {
      font-size: 11px; color: var(--subtext); text-transform: uppercase;
      letter-spacing: 1px; margin-bottom: 12px; display: flex; align-items: center; gap: 6px;
    }
    .card-title::before {
      content: ''; width: 3px; height: 12px; background: var(--cyan); border-radius: 2px;
    }
    .score-card { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 18px; }
    .score-ring-wrap { position: relative; width: 130px; height: 130px; }
    .score-ring-wrap svg { transform: rotate(-90deg); }
    .score-number { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; }
    .score-number .num { font-size: 34px; font-weight: bold; line-height: 1; }
    .score-number .grade { font-size: 12px; margin-top: 4px; color: var(--subtext); }
    .score-dims { width: 100%; display: flex; flex-direction: column; gap: 6px; }
    .dim-row { display: flex; align-items: center; gap: 8px; }
    .dim-label { width: 72px; font-size: 11px; color: var(--subtext); text-align: right; }
    .dim-bar-wrap { flex: 1; height: 6px; background: #21262d; border-radius: 3px; overflow: hidden; }
    .dim-bar { height: 100%; border-radius: 3px; transition: width 0.5s ease; }
    .dim-val { width: 38px; font-size: 11px; color: var(--subtext); text-align: right; }
    #root-cause-badge {
      display: inline-flex; padding: 6px 14px; border-radius: 20px; font-size: 13px;
      font-weight: bold; background: #1a2e1a; color: var(--green); border: 1px solid #2a4a2a;
    }
    #diagnosis-text { font-size: 12px; color: var(--subtext); line-height: 1.75; white-space: pre-wrap; margin-top: 8px; }
    .quick-row { display: flex; flex-wrap: wrap; gap: 14px; font-size: 11px; color: var(--subtext); margin-top: 10px; }
    .quick-row .hl { color: var(--text); }
    .metrics-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
    .metric-card { text-align: center; padding: 14px 12px; }
    .metric-label { font-size: 11px; color: var(--subtext); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
    .metric-value { font-size: 28px; font-weight: bold; line-height: 1; }
    .metric-unit { font-size: 11px; color: var(--subtext); margin-top: 4px; }
    .metric-sub { font-size: 11px; color: var(--subtext); margin-top: 6px; }
    .seq-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
    .seq-item { text-align: center; padding: 10px 6px; background: #1a1f27; border-radius: 6px; }
    .seq-item .label { font-size: 10px; color: var(--subtext); margin-bottom: 4px; }
    .seq-item .val { font-size: 18px; font-weight: bold; }
    .charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .charts-row-full { grid-template-columns: 1fr; }
    .chart-wrap { height: 200px; background: #1a1f27; border-radius: 6px; padding: 8px; }
    .chart-hint { font-size: 10px; color: var(--subtext); margin-top: 6px; }
    canvas { display: block; width: 100%; height: 184px; }
    .path-box { font-size: 11px; color: var(--subtext); word-break: break-all; line-height: 1.6; background: #1a1f27; border-radius: 6px; padding: 10px 12px; }
    footer {
      border-top: 1px solid var(--border); padding: 10px 24px;
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
      gap: 8px; color: var(--subtext); font-size: 11px;
    }
    #fetch-error { flex-basis: 100%; font-size: 10px; color: var(--red); word-break: break-all; }
    .c-green { color: var(--green); } .c-yellow { color: var(--yellow); } .c-red { color: var(--red); }
    .c-cyan { color: var(--cyan); } .c-sub { color: var(--subtext); }
    @media (max-width: 1100px) {
      .health-row, .charts-row { grid-template-columns: 1fr; }
      .metrics-row { grid-template-columns: repeat(2, 1fr); }
    }
    @media (max-width: 640px) {
      .metrics-row, .seq-grid { grid-template-columns: 1fr; }
      header { flex-direction: column; align-items: flex-start; gap: 10px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-left">
      <div class="logo">LiDAR <span>连续取数</span></div>
      <div class="version-badge">Stream Live</div>
    </div>
    <div class="header-right">
      <span>设备: <span class="hl" id="hdr-device">-</span></span>
      <span>雷达: <span class="hl" id="hdr-host">-</span></span>
      <span>运行: <span class="hl" id="hdr-elapsed">00:00:00</span></span>
      <span>刷新: <span class="hl" id="hdr-refresh">#0</span></span>
      <span id="conn-status"><span id="dot" class="dot waiting"></span><span id="statusText">等待数据...</span></span>
    </div>
  </header>
  <main>
    <section class="health-row">
      <div class="card score-card">
        <div class="card-title">综合健康度</div>
        <div class="score-ring-wrap">
          <svg width="130" height="130" viewBox="0 0 130 130">
            <circle cx="65" cy="65" r="52" fill="none" stroke="#21262d" stroke-width="11"/>
            <circle id="score-arc" cx="65" cy="65" r="52" fill="none" stroke-width="11" stroke-linecap="round"
              stroke="#3fb950" stroke-dasharray="327" stroke-dashoffset="327"/>
          </svg>
          <div class="score-number">
            <div class="num" id="score-num">--</div>
            <div class="grade" id="score-grade">--</div>
          </div>
        </div>
        <div class="score-dims">
          <div class="dim-row"><span class="dim-label">帧完整性</span><div class="dim-bar-wrap"><div class="dim-bar" id="dim-frame" style="width:0%;background:var(--green)"></div></div><span class="dim-val" id="dimv-frame">--</span></div>
          <div class="dim-row"><span class="dim-label">缺包率</span><div class="dim-bar-wrap"><div class="dim-bar" id="dim-loss" style="width:0%;background:var(--cyan)"></div></div><span class="dim-val" id="dimv-loss">--</span></div>
          <div class="dim-row"><span class="dim-label">解析质量</span><div class="dim-bar-wrap"><div class="dim-bar" id="dim-parse" style="width:0%;background:var(--magenta)"></div></div><span class="dim-val" id="dimv-parse">--</span></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">状态诊断</div>
        <div id="root-cause-badge">等待数据...</div>
        <div id="diagnosis-text">连续取数测试运行中，指标每秒从 /metrics.json 刷新。</div>
        <div class="quick-row">
          <span>结果: <span class="hl" id="result">-</span></span>
          <span>期望圈频: <span class="hl" id="expectedHz">-</span> Hz</span>
          <span>缺包阈值: <span class="hl" id="lossLimit">-</span>%</span>
          <span>原始落盘: <span class="hl" id="rawCapture">-</span></span>
        </div>
        <div class="quick-row"><span>更新时间: <span class="hl" id="updated">-</span></span></div>
      </div>
    </section>
    <section class="metrics-row">
      <div class="card metric-card"><div class="metric-label">接收帧数</div><div class="metric-value c-cyan" id="frames">--</div><div class="metric-unit">frames</div><div class="metric-sub">完整圈 <span id="completeSub" class="c-sub">-</span></div></div>
      <div class="card metric-card"><div class="metric-label">缺包率</div><div class="metric-value c-green" id="loss">--</div><div class="metric-unit">%</div><div class="metric-sub">丢失包 <span id="missingSub" class="c-sub">-</span></div></div>
      <div class="card metric-card"><div class="metric-label">扫描频率</div><div class="metric-value c-green" id="scanHz">--</div><div class="metric-unit">Hz</div><div class="metric-sub">期望 <span id="expectedHzSub" class="c-sub">-</span> Hz</div></div>
      <div class="card metric-card"><div class="metric-label">最大帧间隔</div><div class="metric-value c-green" id="maxGap">--</div><div class="metric-unit">ms</div><div class="metric-sub">墙钟圈间隔 <span id="wallGapSub" class="c-sub">-</span></div></div>
    </section>
    <section class="card">
      <div class="card-title">应用层统计</div>
      <div class="seq-grid">
        <div class="seq-item"><div class="label">采样时长</div><div class="val c-cyan" id="elapsed">--</div></div>
        <div class="seq-item"><div class="label">解析错误</div><div class="val" id="parseErrors">--</div></div>
        <div class="seq-item"><div class="label">重复包</div><div class="val c-sub" id="duplicates">--</div></div>
        <div class="seq-item"><div class="label">边界忽略圈</div><div class="val c-sub" id="ignored">--</div></div>
        <div class="seq-item"><div class="label">时间戳间隔</div><div class="val" id="tsGap">--</div></div>
        <div class="seq-item"><div class="label">墙钟间隔</div><div class="val" id="wallGap">--</div></div>
        <div class="seq-item"><div class="label">原始帧捕获</div><div class="val c-sub" id="rawFrames">--</div></div>
        <div class="seq-item"><div class="label">已见圈数</div><div class="val c-sub" id="scansSeen">--</div></div>
      </div>
    </section>
    <section class="charts-row">
      <div class="card"><div class="card-title">扫描频率趋势 (Hz)</div><div class="chart-wrap"><canvas id="chartHz" width="600" height="184"></canvas></div></div>
      <div class="card"><div class="card-title">缺包率趋势 (%)</div><div class="chart-wrap"><canvas id="chartLoss" width="600" height="184"></canvas></div></div>
    </section>
    <section class="charts-row">
      <div class="card"><div class="card-title">最大帧间隔趋势 (ms)</div><div class="chart-wrap"><canvas id="chartGap" width="600" height="184"></canvas></div></div>
      <div class="card"><div class="card-title">圈间隔趋势 (ms)</div><div class="chart-wrap"><canvas id="chartScanInterval" width="600" height="184"></canvas></div><div class="chart-hint">紫=墙钟完整圈间隔 · 蓝=设备时间戳圈间隔（最近两圈）</div></div>
    </section>
    <section class="charts-row charts-row-full">
      <div class="card"><div class="card-title">快照路径</div><div class="path-box" id="rawPath">-</div></div>
    </section>
  </main>
  <footer>
    <span>LiDAR 连续取数实时监控 · 上一请求结束后再拉取（避免并发卡死）</span>
    <span id="footer-time">--</span>
    <span id="fetch-error"></span>
  </footer>
""".encode("utf-8") + (
        f"  <!-- live_dashboard build {_DASHBOARD_JS_VERSION} -->\n"
        f'  <script src="/dashboard.js?v={_DASHBOARD_JS_VERSION}" defer></script>\n'
        "</body>\n</html>\n"
    ).encode("utf-8")


class LiveDashboardSession:
    """Serve the live dashboard and write the latest metrics snapshot."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        model: str,
        host: str,
        bind_host: str = "127.0.0.1",
        preferred_port: int = 8765,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_model = _safe_path_part(model)
        safe_host = _safe_path_part(host.replace(":", "_"))
        self.snapshot_path = self.output_dir / f"stream_live_{safe_model}_{safe_host}_{stamp}.json"
        self.bind_host = bind_host
        self.preferred_port = int(preferred_port)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""
        # 内存快照 + 锁：避免 Windows 下轮询打开 JSON 时 replace 报 WinError 5。
        self._snapshot_lock = threading.Lock()
        self._snapshot_bytes = b'{"status":"waiting","metrics":{}}'

    def start(self) -> "LiveDashboardSession":
        handler = self._handler_class()
        last_error: OSError | None = None
        ports = [self.preferred_port] if self.preferred_port <= 0 else list(range(self.preferred_port, self.preferred_port + 20))
        for port in ports:
            try:
                self.server = ThreadingHTTPServer((self.bind_host, port), handler)
                break
            except OSError as exc:
                last_error = exc
        if self.server is None:
            raise RuntimeError(f"Could not start live dashboard server: {last_error}")
        actual_host, actual_port = self.server.server_address
        self.url = f"http://{actual_host}:{actual_port}/"
        self.thread = threading.Thread(target=self.server.serve_forever, name="stream-live-dashboard", daemon=True)
        self.thread.start()
        atexit.register(self.close)
        self.update({}, status="waiting")
        return self

    def update(self, metrics: dict[str, Any], *, status: str = "running") -> None:
        # 看板不需要 metric_explanations，去掉可明显缩小 JSON、加快浏览器 parse，并降低并发 fetch 时的压力。
        slim_metrics = {k: v for k, v in metrics.items() if k != "metric_explanations"}
        payload = {
            "status": status,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "dashboard_url": self.url,
            "metrics": _sanitize_for_json(slim_metrics),
        }
        text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        )
        data = text.encode("utf-8")
        with self._snapshot_lock:
            self._snapshot_bytes = data
        _persist_json_snapshot(self.snapshot_path, text)

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        session = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                # self.path 含查询串（如 /metrics.json?ts=1），不能只做 startswith/全路径相等判断。
                path_only = urlparse(self.path).path or "/"
                if path_only == "/metrics.json":
                    with session._snapshot_lock:
                        data = session._snapshot_bytes
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path_only in {"/", "/index.html"}:
                    data = _dashboard_html()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path_only == "/dashboard.js":
                    data = _dashboard_js()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler
