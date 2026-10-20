# maker_replica 部署手册

照着这份文档从头做到尾，就能把机器人部署好、跑起来、管起来。不需要看代码。

---

## 一、这是什么

这是一个在 **Polymarket「BTC 5 分钟涨跌」市场**上自动挂单的机器人：

- 每 5 分钟一个市场。机器人**同时挂「涨」和「跌」两边的买单**，只当挂单方（maker），从不主动吃单。
- 两边成交的平均成本合计控制在 0.97 以下。结算时总有一边赢，赢的那边每份赔 1 美元，中间的差价就是利润。
- 每个市场收盘前 5 秒撤掉所有挂单，持仓等结算。结算后自动领回收益，不需要 gas 费。
- 自带风控：每个市场最多花 12 美元；总亏损达到 15 美元就自动停止。
- 用途是**小资金（约 50 美元）实测**这套策略能不能赚钱，不是大资金长期运营。

有两种运行模式：

| 模式 | 是否下真单 | 需要密钥 | 用途 |
|---|---|---|---|
| 模拟盘（paper） | 否 | 不需要 | 检查流程能否跑通 |
| 实盘（live） | 是 | 需要 | 真实测试收益 |

> 模拟盘只能证明程序能跑，**不能代表真实收益**：模拟成交规则比真实情况严格，真实的被动成交它模拟不出来。

---

## 二、部署前必读（安全）

1. **私钥由账户主人本人填写。** 实盘要在服务器的 `.env` 文件里填账户私钥。部署的人做完第 1 到第 6 步即可，第 7 步（填 `.env`）请账户主人亲自登录服务器操作。**私钥不要通过聊天、邮件或文件传递**：拿到私钥的人就能转走账户里的全部资金。
2. **必须用专门给机器人用的 Polymarket 账户。** 机器人启动和停止时，会撤销该账户上的**全部挂单**，包括人工挂的单。
3. `.env` 文件已被 git 忽略，不会被提交，也不会被 `git pull` 覆盖。**但它也不会被备份**，丢了要重新填。

---

## 三、服务器要求

| 项目 | 要求 |
|---|---|
| 地区 | 必须在 **Polymarket 不屏蔽的地区**（美国等受限地区不行）。机器人启动实盘前会请求 `https://polymarket.com/api/geoblock` 自检，被屏蔽会直接拒绝启动 |
| 系统 | Linux（推荐 Ubuntu 22.04 及以上）或 macOS |
| 配置 | 1 核 1G 内存即可；硬盘 1G 可用空间 |
| 网络 | 能访问外网：Polymarket（`polymarket.com`、`clob.polymarket.com` 及其 WebSocket）、Binance 行情（`data-stream.binance.vision`）、GitHub |
| 延迟 | 越低越好，第 5 步会实测 |
| 软件 | `git`、`curl`。Python 不用手动装，下面的 `uv` 会自动装 Python 3.12 |

先确认服务器所在地区没有被屏蔽：

```bash
curl -s https://polymarket.com/api/geoblock
```

返回里有 `"blocked":false` 才能继续。如果是 `true`，换一台其他地区的服务器。

---

## 四、部署步骤

### 第 1 步：安装 git 和 curl（已装可跳过）

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y git curl
```

### 第 2 步：安装 uv（Python 环境管理工具）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc        # 用 zsh 的执行 source ~/.zshrc；或者退出重新登录
uv --version            # 能打印版本号就说明装好了
```

### 第 3 步：拉取代码

```bash
cd ~
git clone https://github.com/wangyuqiang9509/maker_replica.git
cd maker_replica
```

如果仓库是私有的，需要先让仓库主人在 GitHub 上把你加为协作者（Settings → Collaborators），再用你自己的 GitHub 账号登录拉取。

### 第 4 步：运行测试（同时自动安装环境）

```bash
./run.sh test
```

- 第一次运行会打印 `Setting up Python 3.12 venv ...`，自动创建 Python 环境并安装依赖，耗时约 1 分钟。
- 最后看到 `OK` 才算通过。如果出现 `FAILED`，停下来，把输出发给仓库主人。

### 第 5 步：测网络延迟（不需要密钥）

```bash
./run.sh latency
```

它会测本机到 Polymarket 推送、到 Binance 推送的延迟、下单撤单的往返时间。
**撤单往返时间越短越好**：参考钱包撤单约 250 毫秒。如果有几台服务器可选，挑数字最小的那台；明显比别的慢很多的服务器不要用。

### 第 6 步：模拟盘试跑（不下真单，不需要密钥）

```bash
./run.sh start          # 后台启动模拟盘
./run.sh status         # 应显示 running pid xxx
./run.sh logs           # 实时看日志；按 Ctrl+C 只是退出查看，机器人不会停
```

跑 15 分钟左右（至少 3 个 5 分钟市场结算完），然后：

```bash
./run.sh summary        # 能看到 windows settled N 且 N > 0 说明流程正常
./run.sh stop
```

**到这里为止，部署人员的工作就完成了。** 把服务器交给账户主人做第 7 步。

### 第 7 步：填写实盘配置（账户主人本人操作）

```bash
cd ~/maker_replica
cp .env.example .env
chmod 600 .env          # 只有本人能读这个文件
nano .env               # 或 vi .env
```

必填项：

| 变量 | 填什么 | 在哪拿 |
|---|---|---|
| `PM_PRIVATE_KEY` | 机器人专用账户的签名私钥 | 钱包导出 |
| `PM_FUNDER` | Polymarket 钱包地址（proxy / safe / deposit 地址） | polymarket.com 个人页；不确定可以留空，用 SDK 默认值 |
| `PM_RELAYER_API_KEY` | 免 gas 领取收益用的中继密钥 | polymarket.com → Settings → API Keys，创建 Relayer API key |
| `PM_RELAYER_API_KEY_ADDRESS` | 上面那个密钥对应的地址 | 同上，创建时一起给出 |

中继密钥是必填的：50 美元的本金要靠每个市场结算后及时领回收益才能周转，不填机器人会拒绝启动。

账户里要先充好测试资金（USDC，约 50 美元）。

可选参数（不改就用默认值，默认值就是按 50 美元设计的）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `MR_KINDS` | `5m` | 做哪些市场。`5m,15m` 会多占一倍资金 |
| `MR_MAX_MARKET_USD` | `12` | 每个市场最多花多少美元 |
| `MR_MAX_LOSS_USD` | `15` | 总亏损达到多少美元自动停止 |
| `MR_PAIR_CAP` | `0.97` | 两边平均成本合计上限，越低越安全但成交越少 |
| `MR_IMPROVE` | `0` | `0` 和买一排队；`1` 插到买一前面一档（独自站在最前面，任何砸盘先打到我们） |
| `MR_PACE_SECS` | `180` | 第一对成交后，新的单边挂单只能用「预算 × 盘内秒数 ÷ 该值」释放出来的预算；`0` 关闭 |
| `MR_STOP_PAIR` | `0` | 大于 0（如 `1.25`）时：裸仓超过 `MR_STOP_AFTER_SECS` 秒且对手盘卖一 ≥ `MR_STOP_PRICE`，就按这个成本上限亏着补对。只限亏损，不改期望。默认关 |
| `MR_RUN_MINUTES` | 空 | 跑多少分钟后自动停；空表示一直跑到手动停止 |
| `MR_TAKER_REBATE_PCT` | `0.18` | 你账户的返佣档位（Gold 是 0.18），只影响收益报告 |

其余参数（行情推送、撤单灵敏度等）不建议改。

### 第 8 步：启动实盘

```bash
./run.sh start --execute
```

看到 `started pid xxx (--execute)` 就是启动成功。然后确认：

```bash
./run.sh status         # running pid xxx
./run.sh logs           # 能看到 quote（报价）、fill（成交）、settle（结算）等记录
```

---

## 五、日常运维

所有命令都在 `~/maker_replica` 目录下执行。

| 要做什么 | 命令 | 说明 |
|---|---|---|
| 看是否在运行 | `./run.sh status` | 同时显示最近 3 条动作 |
| 看实时日志 | `./run.sh logs` | Ctrl+C 退出查看，机器人不会停 |
| 看盈亏汇总 | `./run.sh summary` | 已结算市场数、花费、盈亏、成本 |
| 看收益分析 | `./run.sh report` | 只对实盘有效，见第六部分 |
| 停止 | `./run.sh stop` | 先撤销所有挂单；已有持仓会**等结算并领回收益后**才退出，可能要几分钟 |
| 启动实盘 | `./run.sh start --execute` | |
| 测网络 | `./run.sh latency` | |
| 跑测试 | `./run.sh test` | |

**更新代码：**

```bash
./run.sh stop
./run.sh status         # 等到显示 not running 再继续
git pull
./run.sh test           # 必须通过
./run.sh start --execute
```

**服务器重启后：** 机器人**不会自动启动**，需要登录服务器手动执行 `./run.sh start --execute`。

**运行记录：** 都在 `runtime/` 目录下，**不要删**，收益报告要用到。

| 文件 | 内容 |
|---|---|
| `runtime/journal.jsonl` | 每一个动作（报价、成交、撤单、结算）的完整记录 |
| `runtime/bot.log` | 程序日志 |
| `runtime/edge_report.json` | `./run.sh report` 生成的收益报告 |

---

## 六、怎么判断策略赚不赚钱

实盘跑 **7 到 14 天**后执行：

```bash
./run.sh report
```

看输出里的 `NET`（已扣手续费、已含返佣的净收益率）和它的 90% 置信区间：

| 结果 | 结论 | 怎么做 |
|---|---|---|
| 区间下限 > 0 | 这个资金规模下策略有效 | 可以考虑加资金 |
| 区间上限 < 0 | 策略无效 | 停掉 |
| 区间跨过 0 | 还看不出来 | 继续跑，最多到 14 天 |

辅助指标（报告里和参考钱包对比）：

- **maker share**：挂单成交占比，越接近参考钱包（约 70%）越好。
- **pair cost**（`./run.sh summary` 里）：两边成本合计，参考钱包中位数约 0.963。
- **10 秒 markout**：明显低于参考钱包的 -0.65 美分/份，说明挂单经常被抢先吃掉（网络慢或撤单不及时）。

---

## 七、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `uv not found` | 没装 uv，或装完没重新加载环境 | 回到第 2 步，执行 `source ~/.bashrc` 或重新登录 |
| `missing in .env: PM_PRIVATE_KEY ...` | `.env` 缺必填项 | 按第 7 步补齐 |
| `refusing to trade: Polymarket geoblock check returned ...` | 服务器所在地区被 Polymarket 屏蔽 | 换服务器。**不要用代理或 VPN 绕过**，违反平台规则，账户可能被封 |
| `refusing to trade: set PM_RELAYER_API_KEY ...` | 没填中继密钥 | 按第 7 步填 |
| `bot exited during startup:` 后面跟错误 | 启动时出错 | 把这几行输出发给仓库主人 |
| `already running` | 已经在跑了 | 先 `./run.sh stop` 再启动 |
| `status` 显示 not running，但没执行过 stop | 触发亏损上限自动停止，或程序出错退出 | 看 `./run.sh logs` 最后几行，或 `tail -5 runtime/journal.jsonl`。有 `loss_limit` 字样就是触发了风控 |
| 跑了很久都没有成交（没有 fill） | 价格上限太紧或行情太单边 | 正常现象，先观察；持续一天以上没成交再联系仓库主人 |
| 停止后账户里还有挂单 | 极少见，停止时网络出错 | 去 polymarket.com 手动撤单 |

---

## 八、项目文件说明

| 文件 | 作用 |
|---|---|
| `run.sh` | 一键运行脚本，所有操作都通过它 |
| `bot.py` | 机器人主程序 |
| `quoting.py` | 报价和仓位计算逻辑 |
| `latency.py` | 网络延迟检测 |
| `fill_edge_report.py` | 收益分析报告 |
| `.env.example` | 配置模板（所有参数及默认值） |
| `.env` | 实际配置，含私钥，**不提交、不外传** |
| `tests/` | 自动化测试 |
| `README.md` | 策略原理与设计说明（英文） |
| `DEPLOY.md` | 本文档 |
