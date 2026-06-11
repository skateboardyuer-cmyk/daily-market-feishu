# 每日行情飞书推送

这个项目使用 GitHub Actions、Python 和飞书自定义机器人 Webhook，每个交易日自动推送纳斯达克指数、美股七巨头和华夏全球科技先锋混合 QDII 基金 `005698` 的涨跌情况到飞书群。

## 项目结构

```text
daily-market-feishu/
├─ .github/
│  └─ workflows/
│     └─ daily_market.yml
├─ src/
│  └─ main.py
├─ requirements.txt
├─ README.md
├─ .env.example
└─ .gitignore
```

## 数据源

- 美股数据：Yahoo Finance 非官方接口
  - `https://query1.finance.yahoo.com/v7/finance/quote`
  - 标的：`^IXIC,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA`
- 基金数据：东方财富 / 天天基金
  - `https://fund.eastmoney.com/pingzhongdata/005698.js`

如果接口请求失败或字段结构变化，程序会输出明确错误日志，并让 GitHub Actions 任务失败，避免静默漏报。

## 本地运行

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 创建本地环境变量文件：

```bash
cp .env.example .env
```

3. 编辑 `.env`：

```env
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的机器人-token
FEISHU_SECRET=你的机器人签名密钥，没有开启签名时留空
LOG_LEVEL=INFO
```

4. 运行：

```bash
python src/main.py
```

## GitHub Secrets 配置

进入 GitHub 仓库：

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

需要配置：

| Secret 名称 | 是否必填 | 说明 |
| --- | --- | --- |
| `FEISHU_WEBHOOK_URL` | 必填 | 飞书自定义机器人的 Webhook URL |
| `FEISHU_SECRET` | 可选 | 飞书机器人安全设置里的签名密钥。如果机器人没有开启“签名校验”，可以不填 |

## 飞书机器人 Webhook 怎么填

1. 在飞书群里添加「自定义机器人」。
2. 复制机器人提供的 Webhook 地址，格式通常类似：

```text
https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

3. 把完整地址填入 GitHub Secret：`FEISHU_WEBHOOK_URL`。
4. 如果机器人安全设置开启了「签名校验」，复制签名密钥，填入 GitHub Secret：`FEISHU_SECRET`。

不要把 Webhook 或 Secret 写进代码、README 示例以外的文件，尤其不要提交 `.env`。

## GitHub Actions 定时运行

工作流文件：`.github/workflows/daily_market.yml`

当前配置：

```yaml
on:
  schedule:
    - cron: "30 23 * * 1-5"
  workflow_dispatch:
```

GitHub Actions 的 cron 使用 UTC 时间。`30 23 * * 1-5` 对应北京时间周二到周六 07:30，通常覆盖美国市场周一到周五收盘后的早晨推送。

## 手动触发 GitHub Actions

1. 打开 GitHub 仓库页面。
2. 进入 `Actions`。
3. 选择 `Daily Market Feishu Push`。
4. 点击 `Run workflow`。
5. 选择分支后再次点击 `Run workflow`。

## 在 GitHub Actions 里测试

1. 先确认 `FEISHU_WEBHOOK_URL` 已配置。
2. 如开启飞书签名校验，确认 `FEISHU_SECRET` 已配置。
3. 进入 `Actions` 手动运行 `Daily Market Feishu Push`。
4. 打开本次运行记录，查看 `Push market summary to Feishu` 步骤日志。
5. 飞书群应收到一条 text 消息。

## 推送失败优先检查

1. `FEISHU_WEBHOOK_URL` 是否完整、是否复制了多余空格。
2. 飞书机器人是否仍在群里，Webhook 是否被重新生成或禁用。
3. 如果开启签名校验，`FEISHU_SECRET` 是否与飞书后台完全一致。
4. GitHub Actions 的 Secrets 是否配置在当前仓库，而不是其他仓库或组织层级。
5. GitHub Actions 日志里是否出现 Yahoo Finance 或东方财富接口请求失败。
6. 基金接口字段是否变化。若日志提示无法解析 `fS_name`、`fS_code` 或 `Data_netWorthTrend`，需要更新解析逻辑。

## 消息示例

```text
📈 每日美股与基金速览
日期：2026-06-12

【指数】
纳斯达克：19210.33  +1.24%

【美股七巨头】
AAPL：$198.22  -0.64%
MSFT：$470.11  +1.25%
NVDA：$145.20  +3.18%
AMZN：$183.50  +0.82%
GOOGL：$176.90  -0.21%
META：$502.10  +2.04%
TSLA：$182.60  -4.36%

七巨头平均涨跌：+0.30%
领涨：NVDA +3.18%
领跌：TSLA -4.36%

【关注基金】
华夏全球科技先锋混合QDII 005698
最新净值：3.7721
净值日期：2026-06-11
日涨跌：-0.26%

仅供个人记录，不构成投资建议。
```
