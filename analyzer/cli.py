import argparse
import json
from pathlib import Path
import sys

from .client import analyze


def main(argv=None):
    parser = argparse.ArgumentParser(description='Анализ только подготовленного текстового конверта')
    parser.add_argument('--config', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        output = Path(args.output)
        if output.exists():
            raise ValueError('Файл ответа уже существует, повторный запрос не выполнен')
        bundle = json.loads(Path(args.input).read_text(encoding='utf-8-sig'))
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        result = analyze(bundle, config)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'status': 'analyzed', 'findings_count': len(result['findings'])}))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f'Ошибка анализа: {error if isinstance(error, ValueError) else type(error).__name__}',
              file=sys.stderr)
        return 1
