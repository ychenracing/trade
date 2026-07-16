# A股量化交易系统

> 多策略趋势跟踪 | 200万资金 | 最大回撤20% | 无杠杆

## 策略组合

三个趋势跟踪策略投票表决，至少2票一致才执行交易：

| 策略 | 买入条件 | 卖出条件 |
|------|---------|---------|
| **EMA均线交叉** | EMA10上穿EMA30 + 价格在EMA60上方 + 放量 | EMA10下穿EMA30 或 跌破EMA60 |
| **唐奇安通道突破** | 突破20日最高价 + 突破幅度>ATR×0.5 | 跌破10日最低价 |
| **MACD趋势确认** | MACD金叉 + 柱状图转正 + 价格在MA60上方 | MACD死叉 或 柱状图连续缩短 |

## 风控体系（V2）

| 风控指标 | 阈值 | 说明 |
|---------|------|------|
| 最大总回撤 | 20% (40万) | 触发全局熔断，回撤回落至10%以下自动解除 |
| ATR自适应止损 | 2.5×ATR (8%~15%) | 根据波动率动态调整止损幅度 |
| 单日亏损限额 | 5% (10万) | 触发后暂停，次日自动恢复 |
| 分级移动止盈 | 盈利>150%回吐40%止盈 | 盈利越高回吐阈值越紧 |
| 最大持仓 | 8只 | 分散风险 |
| 单股最大仓位 | 30% (60万) | 趋势感知：up=满仓, mixed=6折, down=4折 |
| 高位降仓 | 涨幅>200%降20% | 涨幅>300%降40% |
| 突破确认加码 | 盈利8%后加仓50% | 最多加2次，空头趋势不加仓 |

## 快速开始

### 1. 安装依赖

```bash
cd quant_trader
pip install -r requirements.txt
```

### 2. 回测验证

```bash
# 回测最近一年
python main.py --mode backtest --start 20230601 --end 20240601
```

### 3. 模拟盘运行

```bash
python main.py --mode paper
```

### 4. 实盘交易（需QMT客户端）

```bash
python main.py --mode live --account YOUR_QMT_ACCOUNT_ID
```

⚠️ **实盘风险提示：**
- 实盘前务必充分回测和模拟盘验证
- 确认QMT客户端已连接并开启交易接口
- 建议先用小资金试运行
- 量化交易有风险，过往表现不代表未来收益

## 项目结构

```
quant_trader/
├── config/              # 配置
│   ├── settings.py      # 资金、风控、交易参数
│   └── strategy_config.py # 策略参数
├── data/                # 行情数据
│   └── data_fetcher.py  # akshare数据源
├── strategy/            # 策略引擎
│   ├── base.py          # 策略基类
│   ├── ma_cross.py      # EMA均线交叉
│   ├── donchian.py      # 唐奇安通道突破
│   ├── macd_trend.py    # MACD趋势确认
│   └── combo.py         # 多策略组合表决
├── risk/                # 风控
│   ├── manager.py       # 风控管理器
│   └── position_sizer.py # 仓位管理
├── execution/           # 交易执行
│   ├── simulator.py     # 模拟盘（含T+1、手续费）
│   └── broker.py        # QMT实盘接口
├── backtest/            # 回测
│   └── engine.py        # 回测引擎
├── monitor/             # 监控通知
│   └── notifier.py      # 交易/风控通知
├── storage/             # 数据存储
│   └── models.py        # SQLite数据库
├── utils/               # 工具
│   ├── logger.py        # 日志系统
│   └── helpers.py       # A股交易规则、技术指标
├── tests/               # 单元测试
│   └── test_strategy.py
├── main.py              # 主程序
└── requirements.txt
```

## 配置说明

### 修改资金和风控参数

编辑 `config/settings.py`:

```python
INITIAL_CAPITAL = 2_000_000.0     # 初始资金
RISK_CONFIG = {
    "max_total_drawdown": 0.25,   # 最大回撤25%
    "max_position_loss": 0.08,    # 单笔止损8%
    ...
}
```

### 修改策略参数

编辑 `config/strategy_config.py`:

```python
EMA_CROSS_CONFIG = {
    "params": {
        "ema_short": 10,    # 调整短期均线周期
        "ema_long": 30,
        "ema_trend": 60,
    }
}
```

### 更换股票池

编辑 `config/settings.py`:

```python
STOCK_UNIVERSE = {
    "pool": "hs300",    # hs300=沪深300 / zz500=中证500 / all=全部A股
}
```

## 运行流程

```
每个交易日:
  9:25  → 集合竞价准备
  9:30  → 开盘，开始交易循环
         ├─ 获取实时行情
         ├─ 检查风控（回撤、日内亏损）
         ├─ 检查持仓止损/止盈
         ├─ 扫描股票池，策略生成信号
         ├─ 执行买卖（T+1限制）
         └─ 每5分钟循环
  15:00 → 收盘
  15:05 → 收盘处理（保存净值、持仓、交易记录）
```

## 回测绩效指标

回测完成后输出：
- 总收益率、年化收益率
- 最大回撤
- 夏普比率
- 胜率、盈亏比
- 净值曲线（导出CSV）

## 技术说明

### A股交易规则
- T+1交易（当日买入次日才能卖出）
- 最小100股/手
- 涨跌停限制（主板10%，科创/创业板20%，ST 5%）
- 交易费用：佣金(万三) + 印花税(千一，卖出) + 过户费(万分之0.2)

### 数据源
- 免费数据：akshare（无需注册）
- 支持缓存，减少重复请求

### 实盘接口
- 支持QMT（迅投）交易接口
- 需券商支持QMT并开通API权限
- 安装xtquant: `pip install xtquant`
