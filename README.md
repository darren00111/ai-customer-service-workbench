# AI 客服工作台

面向电商商家的 Windows 桌面客服工具。当前版本 **1.12.0-preview**，支持无店铺模拟体验、按店铺隔离的知识管理、试聊、一键改正答案及商品价格和库存更新。

日常流程：**问一句 → 填正确内容 → 保存并生效**。系统自动检查更新；检查未通过时保留原资料。复杂管理工具默认折叠。

## 使用说明

在 Windows 上打开完整客户端文件夹，双击 **“开始试聊与改正.cmd”**。选择一个模拟店铺，在左侧输入问题。普通答案有误时，在右侧选中答错的消息，填写正确答案，点击 **“保存并生效”**；看到成功提示后，再问一次确认。库存或售价有误时，切换修改类型，选择商品或规格并填写实际数字；卖完库存填 **0**，补货后再更新数量。点击“查看已学会的资料”可检查现有知识。模拟试聊无需店铺账号或 API Key，不会向顾客发送消息。**本仓库目前提供源码，源码启动方法见下文“本地运行”。**

## 本地运行

需要 Windows 和 Python 3.10 或更高版本。在项目根目录打开 PowerShell：

```powershell
python -m venv feige-agent/.venv
feige-agent/.venv/Scripts/python.exe -m pip install -r feige-agent/requirements.txt
feige-agent/.venv/Scripts/python.exe feige-agent/desktop_main.py --lab
```

`--lab` 打开简化试聊工作台；`--experience` 打开独立的无账号体验。均可选择构造的模拟店铺，无需提供真实店铺数据或 API Key。离线规则效果不代表真实模型的语义质量。

## 目录

- `feige-agent/`：客户端、知识和接待服务、界面及测试。
- `feige-fashion-customer-service/`：本地商品资料读取与分类模块、合成女装样例。
- `tools/prepare_github_upload.py`：发布清单和密钥检查，仅使用 Python 标准库。

程序构建及更多说明见 [客户端说明](feige-agent/README.md)。日常纠错见 [简化纠错说明](feige-agent/docs/简化纠错说明.md)。

## 检查

```powershell
feige-agent/.venv/Scripts/python.exe tools/prepare_github_upload.py --check
feige-agent/.venv/Scripts/python.exe feige-agent/tests/selftest_merchant_edits.py
```

本轮发布前，合成数据离线回归 448 项、浏览器检查 102 项、打包后自检 37 项通过。真实平台接入、真实模型质量、新手参与者和长时间负载尚未完成整体验收。

本仓库从已清理的当前源码快照初始化，未上传本机店铺、会话、账号配置、浏览器缓存、旧备份、安装包或原有本地 Git 历史。模型凭证通过客户端在本机单独配置，不随源码提供。
