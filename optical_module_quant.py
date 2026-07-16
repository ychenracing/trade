#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股光模块股票量化交易策略
基于真实历史数据回测 (2025.04-2026.06)
目标：收益 800%+，回撤 < 20%
持仓数量：6 只
"""

import backtrader as bt
import pandas as pd
import json
import numpy as np
from datetime import datetime

# 加载真实历史数据
def load_real_data():
    """从 JSON 文件加载真实历史数据"""
    with open('/workspace/stock_data_complete.json', 'r', encoding='utf-8') as f:
        all_data = json.load(f)
    return all_data

def prepare_dataframe(stock_data):
    """将股票数据转换为 backtrader 需要的格式"""
    data = stock_data['data']
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    
    # 确保数值类型正确
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    
    return df

class MomentumSelector(bt.Strategy):
    """
    龙头集中持仓策略 - 针对真实数据优化
    - 集中持仓 6 只股票，重仓龙头
    - 宽松止损：8% 止损，避免被正常波动震出
    - 极宽松止盈：让利润充分奔跑
    - 动量选股：优先持有最强龙头
    """
    
    params = (
        ('max_positions', 6),           # 最大持仓数量
        ('momentum_period', 10),        # 动量周期 10 日
        ('stop_loss_pct', 0.08),        # 止损 8% (宽松，避免被震出)
        ('take_profit_pct', 3.00),      # 固定止盈 300% (极宽松)
        ('trailing_start_pct', 0.80),   # 移动止盈启动阈值 80%
        ('trailing_pct', 0.20),         # 移动止盈回撤 20%
        ('ma_fast', 10),                # 快速均线
        ('ma_slow', 30),                # 慢速均线
        ('min_momentum', 15),           # 最小动量要求 15%
    )
    
    def __init__(self):
        self.order_dict = {}            # 订单跟踪
        self.buy_price_dict = {}        # 买入价格
        self.high_water_mark = {}       # 最高水位（用于移动止盈）
        
        # 为每个数据添加指标
        self.momentum = {}
        self.ma_fast = {}
        self.ma_slow = {}
        self.rsi = {}
        
        for d in self.datas:
            idx = d._name
            # 动量指标
            self.momentum[idx] = (d.close / d.close(-self.params.momentum_period) - 1) * 100
            # 均线
            self.ma_fast[idx] = bt.ind.SMA(d.close, period=self.params.ma_fast)
            self.ma_slow[idx] = bt.ind.SMA(d.close, period=self.params.ma_slow)
            # RSI
            self.rsi[idx] = bt.ind.RSI(d.close, period=14)
    
    def notify_order(self, order):
        """订单状态通知"""
        if order.status in [order.Completed]:
            if order.isbuy():
                self.buy_price_dict[order.data._name] = order.executed.price
                self.high_water_mark[order.data._name] = order.executed.price
            elif order.issell():
                if order.data._name in self.buy_price_dict:
                    del self.buy_price_dict[order.data._name]
                if order.data._name in self.high_water_mark:
                    del self.high_water_mark[order.data._name]
        
        self.order_dict[order.data._name] = order
    
    def next(self):
        """主逻辑"""
        # 更新最高水位和检查止盈止损
        for d in self.datas:
            name = d._name
            if name in self.buy_price_dict:
                current_price = d.close[0]
                
                # 更新最高水位
                if current_price > self.high_water_mark[name]:
                    self.high_water_mark[name] = current_price
                
                buy_price = self.buy_price_dict[name]
                high_water = self.high_water_mark[name]
                
                # 计算当前收益
                profit_pct = (current_price - buy_price) / buy_price
                
                # 止损检查 (更严格)
                if profit_pct < -self.params.stop_loss_pct:
                    self.sell(data=d)
                    continue
                
                # 固定止盈检查 (更宽松，让利润奔跑)
                if profit_pct > self.params.take_profit_pct and d.close[0] < d.close[-1]:
                    self.sell(data=d)
                    continue
                
                # 移动止盈检查
                if profit_pct > self.params.trailing_start_pct:
                    trailing_stop = high_water * (1 - self.params.trailing_pct)
                    if current_price < trailing_stop:
                        self.sell(data=d)
                        continue
        
        # 获取可用资金和当前持仓
        cash = self.broker.getcash()
        positions = len([d for d in self.datas if d._name in self.buy_price_dict])
        
        # 如果持仓已满，不再开新仓
        if positions >= self.params.max_positions:
            return
        
        # 计算可开仓数量
        slots_available = self.params.max_positions - positions
        if slots_available <= 0:
            return
        
        # 选股：按动量排序，选择最强的股票
        candidates = []
        for d in self.datas:
            name = d._name
            
            # 跳过已持仓的股票
            if name in self.buy_price_dict:
                continue
            
            # 检查是否有足够数据
            if len(d.close) < self.params.ma_slow:
                continue
            
            # 过滤条件
            momentum_val = self.momentum[name][0]
            ma_fast_val = self.ma_fast[name][0]
            ma_slow_val = self.ma_slow[name][0]
            
            # 在上升趋势中买入
            if ma_fast_val <= ma_slow_val:
                continue
            
            # 动量必须为正且强劲
            if momentum_val < self.params.min_momentum:  # 要求 10 日动量至少 15%
                continue
            
            # 价格不能离高点太远
            if len(d.high) >= 30:
                highest_30 = max([d.high[i] for i in range(-29, 1)])
                if d.close[0] < highest_30 * 0.80:  # 要求在 30 日高点的 80% 以上
                    continue
            
            candidates.append((name, d, momentum_val))
        
        # 按动量排序，选择最强的
        candidates.sort(key=lambda x: x[2], reverse=True)
        
        # 开仓
        num_to_buy = min(slots_available, len(candidates))
        position_size = cash / num_to_buy if num_to_buy > 0 else 0
        
        for i in range(num_to_buy):
            name, d, mom = candidates[i]
            # 使用限价单，价格为当前收盘价
            size = int(position_size / d.close[0] / 100) * 100  # 100 股的整数倍
            if size >= 100:
                self.buy(data=d, size=size)


def run_backtest():
    """运行回测"""
    print("=" * 70)
    print("A 股光模块股票量化交易策略回测")
    print("时间范围：2025.04 - 2026.06")
    print("数据来源：Baostock 真实不复权价格")
    print("目标：收益 800%+, 回撤 < 20%")
    print("最大持仓：6 只")
    print("=" * 70)
    
    # 加载数据
    all_data = load_real_data()
    
    # 创建 Cerebro 引擎
    cerebro = bt.Cerebro()
    
    # 设置初始资金
    cerebro.broker.setcash(1000000.0)  # 100 万初始资金
    cerebro.broker.setcommission(commission=0.0003)  # 万分之三手续费
    
    # 添加数据
    data_feeds = []
    for name, stock_data in all_data.items():
        df = prepare_dataframe(stock_data)
        
        # 检查数据有效性
        if len(df) < 30:
            print(f"警告：{name} 数据不足，跳过")
            continue
        
        data = bt.feeds.PandasData(
            dataname=df,
            name=name,
            datetime=None,
            open='open',
            high='high',
            low='low',
            close='close',
            volume='volume',
            openinterest=-1
        )
        cerebro.adddata(data)
        data_feeds.append(name)
    
    print(f"\n加载了 {len(data_feeds)} 只股票数据:")
    for name in data_feeds:
        ret = all_data[name]['total_return']
        print(f"  - {name}: {ret:.2f}%")
    
    # 添加策略
    cerebro.addstrategy(MomentumSelector)
    
    # 添加分析器
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', riskfreerate=0.02)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    
    # 运行回测
    print("\n开始回测...")
    results = cerebro.run()
    strat = results[0]
    
    # 获取结果
    final_value = cerebro.broker.getvalue()
    initial_value = 1000000.0
    total_return = (final_value - initial_value) / initial_value * 100
    
    # 获取分析结果
    returns = strat.analyzers.returns.get_analysis()
    sharpe = strat.analyzers.sharpe.get_analysis()
    drawdown = strat.analyzers.drawdown.get_analysis()
    trades = strat.analyzers.trades.get_analysis()
    
    # 打印结果
    print("\n" + "=" * 70)
    print("回测结果")
    print("=" * 70)
    print(f"初始资金：¥{initial_value:,.2f}")
    print(f"最终资金：¥{final_value:,.2f}")
    print(f"总收益率：{total_return:.2f}%")
    print(f"目标收益率：800% {'✅ 达标' if total_return >= 800 else '❌ 未达标'}")
    
    if 'sharperatio' in sharpe:
        print(f"夏普比率：{sharpe['sharperatio']:.2f}")
    
    print(f"最大回撤：{drawdown['max']['drawdown']:.2f}%")
    print(f"回撤目标：< 20% {'✅ 达标' if drawdown['max']['drawdown'] < 20 else '❌ 未达标'}")
    
    if 'len' in trades:
        total_trades = trades['total']['total']
        won_trades = trades['won']['total'] if 'won' in trades else 0
        win_rate = won_trades / total_trades * 100 if total_trades > 0 else 0
        print(f"交易次数：{total_trades}")
        print(f"胜率：{win_rate:.1f}%")
    
    # 判断是否达到目标
    success = total_return >= 800 and drawdown['max']['drawdown'] < 20
    print("\n" + "=" * 70)
    if success:
        print("🎉 策略成功达到目标！")
        print(f"   收益率 {total_return:.2f}% >= 800%")
        print(f"   最大回撤 {drawdown['max']['drawdown']:.2f}% < 20%")
    else:
        print("⚠️ 策略未达到目标，需要继续优化")
        if total_return < 800:
            print(f"   收益率 {total_return:.2f}% < 800%")
        if drawdown['max']['drawdown'] >= 20:
            print(f"   最大回撤 {drawdown['max']['drawdown']:.2f}% >= 20%")
    print("=" * 70)
    
    return {
        'total_return': total_return,
        'max_drawdown': drawdown['max']['drawdown'],
        'sharpe_ratio': sharpe.get('sharperatio', 0),
        'success': success
    }

if __name__ == '__main__':
    result = run_backtest()
