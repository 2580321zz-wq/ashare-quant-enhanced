# A股量化增强版

## 本地运行
```bash
pip install -r requirements.txt
streamlit run app.py
```

## GitHub + Streamlit Community Cloud 部署
1. 在 GitHub 新建一个仓库，例如 `ashare-quant-enhanced`。
2. 上传 `app.py`、`requirements.txt`、`README.md`。
3. 登录 Streamlit Community Cloud，选择 **New app**。
4. 选择该仓库、分支（通常 `main`）和主文件路径 `app.py`。
5. 点击 Deploy。
6. 打开应用后先上传真实日线 CSV，或点击合成演示数据测试界面。

## 行情 CSV 格式
必需列：`date,code,open,high,low,close,volume`；建议提供 `amount`，可选 `name`。
示例：
```csv
date,code,name,open,high,low,close,volume,amount
2025-01-02,600000,示例名称,10.1,10.3,10.0,10.2,1200000,12240000
```

## 本地文件和云端注意事项
账户状态保存于 `quant_data/paper_account.json`，成交记录保存于 `quant_data/paper_trades.csv`，行情保存于 `quant_data/market_data.csv`。Streamlit Community Cloud 的本地文件不应视为永久存储；重启或重新部署时可能丢失。请定期下载账户 JSON 备份与成交 CSV。

自动模拟仅在应用进程持续运行且获得新交易日行情时才会处理一次；页面自动刷新不是可靠的后台定时任务。系统不连接券商、不执行真实交易。
