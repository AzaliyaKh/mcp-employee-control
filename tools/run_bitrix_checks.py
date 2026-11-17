"""Offline Linux checks for the new pipeline; mounts test code only, no data/auth."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyzer.cli_runner import docker_executable


def main():
    folder = ROOT / 'data' / ('bitrix-checks-' + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    args = [docker_executable(), 'run', '--rm', '--pull=never', '--network=none', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--tmpfs', '/tmp:mode=1777,size=268435456',
            '--mount', f'type=bind,source={ROOT / "tests"},target=/checks,readonly',
            'ai-service', 'python', '-m', 'unittest', 'discover', '-s', '/checks', '-p', 'test_bitrix*.py', '-v']
    # ai-service is built explicitly by this project's Dockerfile. No real webhook or auth volume is passed.
    try:
        result = subprocess.run(args, cwd=ROOT, capture_output=True, timeout=180)
        text = (result.stdout + result.stderr).decode('utf-8', errors='replace')
        (folder / 'report.txt').write_text(text, encoding='utf-8')
        match = re.search(r'Ran (\d+) tests?', text)
        skipped = re.search(r'skipped=(\d+)', text)
        report = {'success': result.returncode == 0, 'tests': int(match[1]) if match else None,
                  'skipped': int(skipped[1]) if skipped else 0,
                  'time_utc': datetime.now(timezone.utc).isoformat(),
                  'synthetic_data_only': True, 'external_services_called': False}
        (folder / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('BITRIX_CHECKS_SUMMARY=' + json.dumps(report))
        print('REPORT_FOLDER=' + str(folder))
        return result.returncode
    except (OSError, subprocess.TimeoutExpired):
        print('Docker-проверка не выполнена: проверьте Docker Desktop и образ ai-service.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
