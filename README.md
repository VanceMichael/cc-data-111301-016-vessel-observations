# AIS 船舶资料的分片身份与静动态观测资料工具

这个 Python 项目用于业务来源资料的检查、原始字节归档，以及本地合成 AIS 资料的分片接收、解码与按船查询。`vessel_observations/core.py` 校验 NMEA 包络，`ais.py` 解码六位编码，`fragments.py` 按来源作用域组装分片，`receiver.py` 建立观测索引与查询视图，`archive.py` 保存原文件。输入全部是本地资料文件，不连接无线电设备。

## 环境

Python 3.11.2。依赖及其间接依赖固定在 `requirements.txt`；空清单表示只使用标准库。安装只在环境准备阶段执行：

```sh
python3 -m pip install -r requirements.txt
```

## 运行

```sh
python3 -m vessel_observations inspect fixtures/station-a.nmea
python3 -m vessel_observations capture fixtures/station-a.nmea --archive .work/raw
python3 -m vessel_observations receive fixtures/connections.json
python3 -m vessel_observations query fixtures/connections.json 412000001
python3 -m vessel_observations demo
```

`receive` 默认按行轮转两个岸站，模拟交错发送；`--no-interleave` 改为按清单顺序逐份读取。`--stale-after N` 设置半包过期期限（以同一来源连接后续到达的片段数计）。所有命令输出 JSON；输入不可识别、校验不符或连接清单不完整时命令失败，不产生空的成功结果。查询不存在的 MMSI 返回 `found: false` 及原因。

分片组的作用域、异常处置、支持的报文类型和查询字段的出处说明见 [docs/data.md](docs/data.md)。

## 本地合成资料

`fixtures/` 中的两个岸站资料由 `fixtures/generate.py` 用 `tests/synthetic.py` 生成，全部为虚构船舶与位置：

```sh
python3 -m fixtures.generate
```

两个岸站故意使用相同的顺序号 7 和频道 A；接收结果必须保持为两条独立观测。任何示例身份与业务数值均为虚构，不需要外部账号。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q vessel_observations tests fixtures
```
