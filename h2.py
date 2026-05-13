from pathlib import Path
import importlib.util
import runpy
import sys

TARGET = Path(__file__).resolve().parent / "码盘补偿"/ "h2_resolution_gui_test.py"

if __name__ == "__main__":
    sys.path.insert(0, str(TARGET.parent))
    runpy.run_path(str(TARGET), run_name="__main__")
else:
    spec = importlib.util.spec_from_file_location(__name__, TARGET)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    globals().update(module.__dict__)
