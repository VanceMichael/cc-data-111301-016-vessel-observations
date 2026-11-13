# 来源资料约定

来源是 NMEA AIVDM 句子，星号后为 XOR 校验。外层 connections.json 给出岸站和连接身份，原始 NMEA 顺序号并非全局唯一。station-a 与 station-b 为两路独立来源；相同句子在另一岸站出现不自动代表同一次接收。静态字段和位置字段分别来自不同消息类型。
