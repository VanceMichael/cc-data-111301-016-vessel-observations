DESCRIPTION = 'AIS 船舶资料的分片身份与静动态观测接收'
SAMPLE = 'fixtures/station-a.nmea'
CONNECTIONS = 'fixtures/connections.json'

import argparse
import json
from pathlib import Path
import tempfile

from .archive import archive_file
from .core import inspect_file
from .receiver import query_result, receive_files


def _print(result):
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('command', choices=['inspect', 'capture', 'receive', 'query', 'demo'])
    parser.add_argument('path', nargs='?', default=None)
    parser.add_argument('mmsi', nargs='?', default=None)
    parser.add_argument('--archive', default='.work/raw')
    parser.add_argument('--no-interleave', action='store_true',
                        help='按清单顺序逐份读取，而不是逐行交错两个岸站')
    parser.add_argument('--stale-after', type=int, default=64,
                        help='半包在多少个后续片段后过期（默认为 64）')
    args = parser.parse_args()

    if args.command == 'inspect':
        result = inspect_file(Path(args.path or SAMPLE))
    elif args.command == 'capture':
        result = archive_file(Path(args.path or SAMPLE), Path(args.archive))
    elif args.command in ('receive', 'query'):
        source = Path(args.path or CONNECTIONS)
        if args.command == 'query':
            if args.mmsi is None:
                parser.error('query 需要给出 MMSI')
            result = query_result(receive_files(source, interleave=not args.no_interleave,
                                                stale_after=args.stale_after), args.mmsi)
        else:
            result = receive_files(source, interleave=not args.no_interleave,
                                   stale_after=args.stale_after)
    else:
        source = Path(args.path or SAMPLE)
        with tempfile.TemporaryDirectory() as directory:
            result = {'原始资料': archive_file(source, Path(directory)),
                      '资料预览': inspect_file(source)}
            connections = Path(CONNECTIONS)
            if connections.exists():
                try:
                    received = receive_files(connections)
                    result['分片接收'] = {
                        'assembled': len(received['observations']),
                        'unresolved': received['unresolved'],
                        'unprocessed': received['unprocessed'],
                        'observation_counts': received['observation_counts'],
                    }
                except (OSError, ValueError):
                    result['分片接收'] = {'error': '连接清单不可用'}
    _print(result)


if __name__ == '__main__':
    main()
