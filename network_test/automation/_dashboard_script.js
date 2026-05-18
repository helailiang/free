  <script>
    const history = [];
    const maxPoints = 180;
    let refreshCount = 0;
    const CIRC = 327;
    const $ = (id) => document.getElementById(id);
    const fmt = (v, digits = 3) => Number.isFinite(Number(v)) ? Number(v).toFixed(digits) : "-";
    const setText = (id, value) => { const el = $(id); if (el) el.textContent = value ?? "-"; };
    const fmtElapsed = (sec) => {
      const s = Math.max(0, Math.floor(Number(sec) || 0));
      const h = String(Math.floor(s / 3600)).padStart(2, "0");
      const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
      const ss = String(s % 60).padStart(2, "0");
      return h + ":" + m + ":" + ss;
    };
    function setMetricColor(id, ok) {
      const el = $(id);
      if (!el) return;
      el.className = "metric-value " + (ok ? "c-green" : "c-red");
    }
    function setDim(barId, valId, pct) {
      const p = Math.max(0, Math.min(100, pct));
      $(barId).style.width = p + "%";
      setText(valId, Math.round(p) + "%");
    }
    function updateScore(m) {
      const loss = Number(m.loss_rate_percent || 0);
      const limit = Number(m.stream_loss_limit_percent_applied ?? 0.5);
      const parseErr = Number(m.parse_errors || 0);
      const frames = Number(m.frames_received || 0);
      const frameScore = frames > 0 ? Math.max(0, 100 - parseErr / frames * 100) : 50;
      const lossScore = Math.max(0, 100 - (loss / Math.max(limit, 0.01)) * 50);
      const parseScore = frames > 0 ? Math.max(0, 100 - parseErr * 5) : 80;
      const total = Math.round((frameScore + lossScore + parseScore) / 3);
      const arc = $("score-arc");
      arc.style.strokeDashoffset = String(CIRC * (1 - total / 100));
      arc.style.stroke = total >= 80 ? "#3fb950" : total >= 60 ? "#d29922" : "#f85149";
      setText("score-num", String(total));
      setText("score-grade", total >= 90 ? "优秀" : total >= 75 ? "良好" : total >= 60 ? "一般" : "需关注");
      setDim("dim-frame", "dimv-frame", frameScore);
      setDim("dim-loss", "dimv-loss", lossScore);
      setDim("dim-parse", "dimv-parse", parseScore);
    }
    function pushHistory(m) {
      history.push({
        hz: Number(m.implied_scan_rate_hz || 0),
        loss: Number(m.loss_rate_percent || 0),
        maxGapMs: Number(m.max_inter_frame_gap_s || 0) * 1000,
      });
      if (history.length > maxPoints) history.shift();
    }
    function drawSeries(canvasId, key, color, label) {
      const canvas = $(canvasId);
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.fillStyle = "#1a1f27";
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = "#30363d";
      for (let i = 0; i <= 4; i++) {
        const y = 16 + i * ((h - 32) / 4);
        ctx.beginPath(); ctx.moveTo(36, y); ctx.lineTo(w - 8, y); ctx.stroke();
      }
      const vals = history.map(p => p[key]).filter(v => Number.isFinite(v));
      if (!vals.length) return;
      const max = Math.max(...vals, 0.001) * 1.12;
      ctx.fillStyle = color; ctx.font = "11px Consolas, monospace";
      ctx.fillText(label, 40, 12);
      ctx.strokeStyle = color; ctx.lineWidth = 2;
      ctx.beginPath();
      history.forEach((p, i) => {
        const x = 36 + (history.length <= 1 ? 0 : i * ((w - 48) / (history.length - 1)));
        const y = h - 14 - (p[key] / max) * (h - 36);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    function updateDiagnosis(m, ok) {
      const badge = $("root-cause-badge"), text = $("diagnosis-text");
      if (!badge || !text) return;
      if (ok) {
        badge.textContent = "运行正常";
        badge.style.cssText = "background:#1a2e1a;color:#3fb950;border:1px solid #2a4a2a";
        text.textContent = "缺包率与扫描频率在阈值内，连续取数链路稳定。";
      } else {
        badge.textContent = "需要关注";
        badge.style.cssText = "background:#2e1a1a;color:#f85149;border:1px solid #4a2a2a";
        const loss = Number(m.loss_rate_percent || 0);
        const limit = Number(m.stream_loss_limit_percent_applied ?? 0.5);
        const parts = [];
        if (loss > limit) parts.push("缺包率 " + fmt(loss, 4) + "% 超过阈值 " + fmt(limit, 4) + "%");
        if (Number(m.parse_errors || 0) > 0) parts.push("解析错误 " + m.parse_errors + " 次");
        text.textContent = parts.length ? parts.join("\n") : "部分指标偏离期望，请结合原始抓包排查。";
      }
    }
    async function refresh() {
      try {
        const res = await fetch("/metrics.json?ts=" + Date.now(), { cache: "no-store" });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const payload = await res.json();
        const m = payload.metrics || {};
        const status = payload.status || "running";
        refreshCount += 1;
        $("dot").className = "dot " + (status === "final" ? "done" : status === "waiting" ? "waiting" : "running");
        setText("statusText", status === "final" ? "采样结束" : status === "waiting" ? "等待数据..." : "采集中");
        setText("hdr-device", m.model || "-");
        setText("hdr-host", m.host || "-");
        setText("hdr-elapsed", fmtElapsed(m.window_duration_s));
        setText("hdr-refresh", "#" + refreshCount);
        setText("footer-time", new Date().toLocaleString());
        const limit = Number(m.stream_loss_limit_percent_applied ?? 0.5);
        const ok = Number(m.loss_rate_percent || 0) <= limit;
        setText("updated", payload.updated_at || "-");
        setText("result", ok ? "PASS" : "CHECK");
        setText("expectedHz", fmt(m.expected_scan_rate_hz, 2));
        setText("expectedHzSub", fmt(m.expected_scan_rate_hz, 2));
        setText("lossLimit", fmt(limit, 4));
        setText("rawCapture", m.raw_capture_truncated ? "truncated" : (m.raw_capture_path ? "enabled" : "-"));
        setText("elapsed", fmt(m.window_duration_s, 2) + " s");
        setText("frames", m.frames_received ?? "-");
        setText("completeSub", m.completed_scans ?? "-");
        setText("scansSeen", m.scans_seen ?? m.completed_scans ?? "-");
        setText("loss", fmt(m.loss_rate_percent, 4));
        setMetricColor("loss", ok);
        setText("missingSub", m.missing_packets ?? "-");
        setText("scanHz", fmt(m.implied_scan_rate_hz, 3));
        setText("maxGap", fmt(Number(m.max_inter_frame_gap_s || 0) * 1000, 2));
        setText("tsGap", m.scan_timestamp_interval_avg_display || "-");
        setText("wallGap", m.completed_scan_wall_interval_avg_display || "-");
        setText("wallGapSub", m.completed_scan_wall_interval_avg_display || "-");
        setText("parseErrors", m.parse_errors ?? "-");
        setText("duplicates", m.duplicate_packets ?? "-");
        setText("ignored", m.boundary_partial_scans_ignored ?? "-");
        setText("rawFrames", m.raw_frames_captured ?? "-");
        setText("rawPath", m.raw_capture_path || payload.dashboard_url || "-");
        updateScore(m);
        updateDiagnosis(m, ok);
        pushHistory(m);
        drawSeries("chartHz", "hz", "#58a6ff", "Hz");
        drawSeries("chartLoss", "loss", "#f85149", "%");
        drawSeries("chartGap", "maxGapMs", "#3fb950", "ms");
      } catch (err) {
        $("dot").className = "dot waiting";
        setText("statusText", "等待数据...");
      }
    }
    refresh();
    setInterval(refresh, 1000);
  </script>
