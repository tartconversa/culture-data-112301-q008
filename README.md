# 跨境设计服务的返工风险与组合报价

用于本地业务开发的合成经营资料，所有参与方均使用虚构标识。

data保存UTF-8 JSONL记录及catalog.json格式登记，docs/资料契约.md说明来源口径、字段、单位及直接引用。不同来源、业务时期与后续到达的记录各自保留身份，不能仅按文件行数解释业务数量。

Python需要3.11至3.14。依赖版本在requirements.txt中固定，可在独立虚拟环境安装。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 tools/check_materials.py
python3 -m unittest discover -s tests -v
```

Windows PowerShell中可用`.venv\Scripts\Activate.ps1`激活虚拟环境。

资料检查入口输出各表行数，验证内容指纹、字段类型及直接外键。现有测试检查资料完整性和损坏检测，其通过只说明资料可读取。data及docs中的记录不包含经营分析结果。应用产生的本地状态可放入state，导出内容可放入outputs，两者已从Git排除。

## 估价应用

estimation/提供接收项目、估价、确认报价与追踪变更的可运行能力，说明见docs/应用说明.md：

```sh
python3 tools/estimate.py
python3 -m unittest discover -s tests -v
```
