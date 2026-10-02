# demo · 本地演示程序

面向演示的本地实测环境。依赖**模拟库 A 的 Wren 语义层**（`127.0.0.1:9000`）运行。

## 启动

```bash
python demo/demo-server.py     # 默认 127.0.0.1:8080
```

## 文件

| 文件 | 用途 |
|---|---|
| `demo-server.py` | 本地实测服务：复用 Wren MCP 客户端协议，加载本地 MDL，提供 `/api/*` REST 并托管实测页面 |
| `demo/数据需求智能分析助手-语义层实测.html` | **实测页面（联机版）**：由 `demo-server.py` 托管，问题实时走语义层执行 |
| `demo/数据需求智能分析助手-语义层实测-演示版.html` | 同上的**静态快照版**：数据已内联，不依赖后端，适合离线演示 |
| `demo/数据需求智能分析助手-V7.html` | 模块化工作台页面（POC 演示参考，其代码不进入正式产品） |
| `serve-preview.py` | 通用静态预览服务（禁缓存），用于快速预览任意 HTML |
| `CheersAI-Logo.png` | 品牌资产，演示页面引用 |

## 说明

- `demo-server.py` 读取的语义层基准是 `../wren-docker/workspace/mdl.json`（模拟库 A）。
- V7 工作台页面属于 POC 阶段演示参考，正式产品前端为自建统一前端，代码不继承。
