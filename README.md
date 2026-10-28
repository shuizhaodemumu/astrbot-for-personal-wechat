# AstrBot for Personal WeChat

让个人微信测试号在指定微信群中接收 **@自己的文字消息**，交给 AstrBot 调用模型，再把文字回复发回原群。

这是一个社区实验性适配器，使用本机运行的 `agent-wechat` 原生服务；不是腾讯官方机器人接口，也不是 AstrBot 官方内置微信适配器。本仓库提供 Python 插件、配置辅助脚本和部署文档，不包含微信客户端、桥接二进制、账号资料或模型密钥。

## 工作方式

```mermaid
flowchart LR
    A[指定微信群中的 @文字消息] --> B[agent-wechat 1.1.0]
    B -->|本机 HTTP API| C[AstrBot 平台适配器]
    C -->|群白名单与明确 @ 检查| D[AstrBot 独立群配置]
    D --> E[已配置的聊天模型]
    E --> D
    D -->|文字回复| C
    C --> B
    B --> A
```

## 当前能力

- 只接收配置中列出的微信群、明确 @当前微信号的文字消息。
- 忽略私聊、其他群、自己发送的消息、历史回放和仅 @全体成员的消息。
- 为尚未绑定独立配置的目标群创建 AstrBot 配置，指定聊天模型，取消额外的模型唤醒前缀；不修改其他平台的默认设置。
- 保留 AstrBot 原有的人设和上下文处理。人设可能改变回复措辞。
- 保存事件游标和已处理消息 ID；发送请求带幂等键，减少重复回复。
- 发送前再次读取原消息，已撤回的消息不继续回复。
- 桥接只连接 `http://127.0.0.1:6174`，认证令牌从本地文件读取。

## 已验证环境

| 组件 | 实测版本 / 范围 |
| --- | --- |
| 操作系统 | Ubuntu、Linux x86_64；测试主机 glibc 2.43 |
| Python | 3.12 |
| AstrBot | 4.29.3，启动器安装的源码运行实例 |
| agent-wechat | npm `@agent-wechat/cli@1.1.0`，原生 `wx` 服务 |
| aiohttp | 3.14.5 |
| 模型 | 阿里云百炼 OpenAI 兼容接口中的 `qwen-flash` |
| 验证日期 | 2026-10-28 |

实测完成：测试号扫码登录 → 接收指定群 @消息 → AstrBot 调用模型 → 微信服务器接受发送 → 群成员确认收到回复。另有 5 项自动测试覆盖准入、去重、撤回检查、发送回执及配置隔离。

以上仅代表这一环境的验证结果。插件依赖 AstrBot 的配置管理接口；其他版本、操作系统或账号状态需要重新验证。

## 开始使用

请按 [Ubuntu 部署步骤](docs/DEPLOYMENT.md) 操作。流程是：

1. 准备 AstrBot，并在它的管理界面配置可正常调用的聊天模型。
2. 独立安装 `agent-wechat` 1.1.0，用本仓库脚本生成私有运行配置。
3. 启动本机桥接服务，用单独的微信测试号扫码确认。
4. 把测试号加入目标群，查询账号 ID、群 ID。
5. 停止 AstrBot，运行安装脚本，指定模型 ID 和群 ID，再启动 AstrBot。
6. 用另一个账号在目标群中 @测试号进行验证。

也可根据 [平台配置示例](examples/platform.example.json) 手动安装。示例中的值全部是占位数据，不能直接运行。

## 目录

```text
main.py                       AstrBot 插件入口与平台适配器
metadata.yaml                 插件信息
requirements.txt              运行依赖
scripts/setup_bridge.py       生成本机桥接配置
scripts/wx                    调用独立安装的桥接 CLI
scripts/install_plugin.py     安装插件、备份并追加平台配置
examples/platform.example.json
tests/test_adapter.py         自动测试
docs/DEPLOYMENT.md             部署、启动、停止、升级与移除
docs/TROUBLESHOOTING.md        故障排查
OPEN_SOURCE.md                构建来源、依赖与许可证说明
LICENSE                       本仓库许可证
```

## 限制与行为边界

- 当前只处理文字；不支持图片、语音、文件和主动群发，也不提供自动加群或批量加好友。
- 模型返回的非文字组件不会发送；流式输出会合并后发送。
- 重启后忽略启动前的消息；超过 5 分钟的消息也不会回复。模型处理过慢时，发送前检查可能丢弃结果。
- 游标在交给 AstrBot 之前落盘，优先避免重复回复。进程恰在落盘后崩溃时，可能漏掉该条消息；不承诺恰好一次投递。
- 发送结果不确定时，不会自动重新提交。需要查看桥接发送记录判断实际状态。
- 上游新建的独立账号资料可能标记 `schemaCompatibility: unverified` 或存在待投影批次。登录成功不等于所有接口都兼容；应以实际收发为准。
- 默认没有开机自启。Ubuntu、桥接和 AstrBot 都需要保持运行。
- 使用个人账号的非官方接入可能遇到微信风控、掉线或协议变更。建议使用单独测试号，并让群成员知晓机器人用途。

## 开发与测试

测试需要已安装 AstrBot 及其依赖。不要把真实微信资料用于测试。

```bash
# 在本仓库目录中，用已有 AstrBot 虚拟环境运行。
# ASTRBOT_CORE 指向包含 astrbot/ 的源码目录。
export ASTRBOT_CORE=/absolute/path/to/AstrBot
export ASTRBOT_PYTHON=/absolute/path/to/venv/bin/python
PYTHONPATH="$ASTRBOT_CORE:$PWD" "$ASTRBOT_PYTHON" -m unittest discover -s tests -v

# 在开发环境另行安装 Ruff 后执行：
ruff check .
ruff format --check .
```

自动测试使用模拟消息与请求，不登录微信、不发群消息、不调用收费模型。

## 来源与许可

本仓库代码采用 **AGPL-3.0-or-later**，见 [LICENSE](LICENSE)。主要依赖与参考项目包括 [AstrBot](https://github.com/AstrBotDevs/AstrBot)、[agent-wechat](https://github.com/thisnick/agent-wechat)、[aiohttp](https://github.com/aio-libs/aiohttp)，以及前期评估过的 [astrbot-plugin-agent-wechat](https://github.com/JackZhaoooooo/astrbot-plugin-agent-wechat)。详细用途、版本及许可证核验范围见 [OPEN_SOURCE.md](OPEN_SOURCE.md)。

**本仓库的许可证不覆盖第三方桥接程序。** 部分公开可读的上游仓库未提供可确认的仓库级许可证，不能据此推定可以任意再分发。本仓库只提供安装指引和链接，不重新分发这些项目。
