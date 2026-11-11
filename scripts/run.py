"""Run a pipeline command, including the preserved denoise runtime.

Example: uv run python scripts/run.py denoise_v2_rollout.py --help
"""
import os
import importlib
import runpy
import subprocess
import sys

from _bootstrap import LAYOUT, ScriptProcesses, source_path


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print('\nCommands by folder:')
        for name, relative in sorted(LAYOUT.items(), key=lambda item:(item[1], item[0])):
            print(f'  {relative}')
        return 0
    name, arguments = sys.argv[1], sys.argv[2:]
    path = source_path(name)
    sys.argv = [str(path), *arguments]
    if path.name == 'denoise_v2_rollout.py':
        # Import through the relocation loader so saved external review paths
        # resolve to the real checkout. The frozen source stays byte-identical.
        for variable in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS',
                         'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
            os.environ[variable] = '1'
        import denoise_v2_rollout
        return denoise_v2_rollout.cli()
    if path.name in ('denoise_v2_cluster.py', 'denoise_v2_accelerated.py'):
        import denoise_v2_cluster as cluster
        module = importlib.import_module(path.stem)
        originals = {item:item.subprocess for item in (cluster, module)}
        for item, original in originals.items():
            item.subprocess = ScriptProcesses(original)
        try:
            return module.main()
        except InterruptedError as exc:
            if path.name == 'denoise_v2_accelerated.py':
                clean = str(exc) in {'Stopped after the last committed checkpoint',
                                    'Stopped before publishing normalized audio'}
                print(f'Worker {"checkpointed" if clean else "interrupted unexpectedly"}: {exc}',
                      file=sys.stderr, flush=True)
                return module.CHECKPOINT_EXIT if clean else 2
            print(f'Cluster stopped: {exc}', file=sys.stderr, flush=True)
            return 2
        except (ValueError, OSError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            prefix = 'Cluster' if path.name == 'denoise_v2_cluster.py' else 'Accelerated worker'
            print(f'{prefix} stopped: {exc}', file=sys.stderr, flush=True)
            return 2
        finally:
            for item, original in originals.items():
                item.subprocess = original
    runpy.run_path(str(path), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
