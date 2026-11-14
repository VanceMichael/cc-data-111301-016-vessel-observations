"""生成本地合成演练资料（不连接任何无线电设备）。

运行：python3 -m fixtures.generate
"""

from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.synthetic import (encode_type_1, encode_type_5, encode_type_18,
                             encode_type_24a, encode_type_24b, encode_unsupported,
                             fragments, single)


def main():
    root = Path(__file__).resolve().parent
    alpha_type5 = fragments(
        encode_type_5(412000001, 'ALPHA VESSEL', 'CALL-A', ship_type=70,
                      bow=100, stern=20, port=8, starboard=8, draught=6.0),
        sequence='7')
    boa_type5 = fragments(
        encode_type_5(412000002, 'BOA VESSEL', 'CALL-B', ship_type=60,
                      bow=40, stern=15, port=5, starboard=5),
        sequence='7')
    # 两个岸站使用相同的顺序号 7 和相同的频道 A；它们必须保持分开。
    station_a = (alpha_type5
                 + single(encode_type_1(412000001, 22.5, 114.2, sog=12.3, cog=35.0,
                                        heading=36, timestamp=42))
                 + single(encode_type_24a(412000001, 'ALPHA VESSEL'), sequence='8'))
    station_b = (boa_type5
                 + fragments(encode_unsupported(21, 412000099), sequence='7')
                 + single(encode_type_18(412000002, 22.4, 114.1, sog=8.0, cog=10.0))
                 + single(encode_type_24b(412000002, 'CALL-B', ship_type=60,
                                          bow=40, stern=15, port=5, starboard=5),
                          sequence='9')
                 + single(encode_type_24a(412000002, 'BOA VESSEL'), sequence='10'))
    # 第三个来源只送来半条静态资料，演示“仍未组装”的原因。
    gamma_type5 = fragments(
        encode_type_5(412000003, 'GAMMA VESSEL', 'CALL-C'), sequence='12')
    station_c = gamma_type5[:1]

    (root / 'station-a.nmea').write_text('\n'.join(station_a) + '\n', encoding='ascii')
    (root / 'station-b.nmea').write_text('\n'.join(station_b) + '\n', encoding='ascii')
    (root / 'station-c.nmea').write_text('\n'.join(station_c) + '\n', encoding='ascii')
    (root / 'connections.json').write_text(json.dumps([
        {'file': 'station-a.nmea', 'station': 'shore-a', 'connection': 1},
        {'file': 'station-b.nmea', 'station': 'shore-b', 'connection': 1},
        {'file': 'station-c.nmea', 'station': 'shore-a', 'connection': 2},
    ], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
