"""Forward to scripts/_bootstrap.py for stage sources run directly from sg-audio/<stage>/.

Stage sources add their parent folder to sys.path and import _bootstrap. They
are hash-bound and stay byte-identical, so this file loads the shared adapter in
their place.
"""
import importlib.util
from pathlib import Path
import sys

_real = Path(__file__).resolve().parents[1]/'_bootstrap.py'
if str(_real.parent) not in sys.path:
    sys.path.insert(1, str(_real.parent))  # stage_storage, archive_storage, script_relocation
_spec = importlib.util.spec_from_file_location('_bootstrap', _real)
_module = importlib.util.module_from_spec(_spec)
sys.modules['_bootstrap'] = _module  # the import statement returns this module
_spec.loader.exec_module(_module)
