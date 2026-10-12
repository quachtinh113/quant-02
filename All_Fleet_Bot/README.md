# UNIFIED QUANT FLEET — 5 Bots Trên 1 PC

Hệ thống điều phối tập trung vận hành đồng thời **5 Bot chiến lược định lượng** trên cùng một máy tính và một tài khoản MetaTrader 5 duy nhất:

| Bot | Tên Chiến Lược | Thị Trường | Khung Thời Gian / Phiên | Magic Number |
|---|---|---|---|:---:|
| **Bot 1** | `BTC_8H` | `BTCUSDm` | Chu kỳ 8 tiếng (00:00, 08:00, 16:00 UTC) | **`260904`** |
| **Bot 2** | `FX_D1` | `EURUSDm`, `GBPUSDm`, `USDJPYm`, `AUDUSDm`, `USDCADm` | Nến ngày D1 đóng (20:00 UTC) | **`260901`** |
| **Bot 3** | `GOLD_SESS` | `XAUUSDm`, `XAGUSDm` | Mở phiên London (08:00) & New York (13:00) | **`260903`** |
| **Bot 4** | `USIDX_SESS` | `US500m`, `USTECm` | Mở (14:00) & Đóng (20:00) phiên chứng khoán Mỹ | **`260902`** |
| **Bot 5** | `NOWTRADING_MTF` | Multi-symbol (`XAUUSDm`, `USDCADm`, `USDCHFm`, v.v.) | Đa khung M15-H1-H4 (RSI + ADX + ATR DCA) | **`260905`** *(kế thừa 202500)* |

---

## 1. Cấu Trúc Thư Mục

```text
D:\05_Quant\ALL_FLEET_BOT/
├── run_unified_bot.py         # 1 FILE BOT DUY NHẤT (Master Orchestrator)
├── start_fleet.bat            # File khởi động 1-click có Watchdog tự restart
├── test_fleet.py              # Bộ kiểm thử tự động toàn diện
├── README.md                       # Hướng dẫn chi tiết sử dụng & vận hành
├── .env                            # Cấu hình thông tin tài khoản MT5
├── config/
│   ├── fleet_config.yaml           # Cấu hình chi tiết 5 bot (bật/tắt, lot, SL, TP)
│   └── magic_registry.py           # Bảng cô lập Magic Number duy nhất, chống xung đột
├── core/
│   ├── mt5_connection_manager.py # Quản lý kết nối MT5 duy nhất, thread-safe, tự auto-reconnect
│   ├── fleet_risk_guard.py    # Kiểm soát rủi ro toàn tài khoản (Max DD, Margin Guard)
│   └── audit_logger.py             # Nhật ký ghi log chuẩn hóa thời gian thực
└── strategies/                     # 5 Module Chiến Lược Độc Lập (Dạng Plug-and-Play)
    ├── base_strategy.py            # Lớp cơ sở định nghĩa vòng đời bot
    ├── bot1_btc_8h.py              # Engine Bot 1
    ├── bot2_fx_d1.py               # Engine Bot 2
    ├── bot3_gold_sess.py           # Engine Bot 3
    ├── bot4_usidx_sess.py          # Engine Bot 4
    └── bot5_nowtrading_mtf.py      # Engine Bot 5
└── logs/
    └── fleet.log              # Nhật ký hoạt động chi tiết tự động xoay vòng
```

---

## 2. Cách Vận Hành

### Bước 1: Mở MetaTrader 5
Mở phần mềm MetaTrader 5 (Exness) trên máy và đăng nhập tài khoản giao dịch.

### Bước 2: Chạy Bot
Bấm đúp chuột vào file:
```cmd
D:\05_Quant\ALL_FLEET_BOT\start_fleet.bat
```
Hoặc mở terminal chạy:
```powershell
cd D:\05_Quant\ALL_FLEET_BOT
py -3.12 run_unified_bot.py
```

### Bước 3: Dừng Khẩn Cấp (Emergency Kill Switch)
Nếu muốn tạm dừng toàn bộ 5 bot không cho mở thêm bất kỳ lệnh nào mới:
Tạo một file có tên `HALT_FLEET` ngay trong thư mục `D:\05_Quant\ALL_FLEET_BOT`. Hệ thống sẽ lập tức khóa toàn bộ lệnh mới. Khi xóa file này, hệ thống sẽ tự động hoạt động bình thường trở lại.

---

## 3. Quản Trị Rủi Ro Tập Trung (Fleet Risk Guard)

Hệ thống bảo vệ toàn diện tài khoản với 3 tầng ngắt mạch:
1. **Bảo vệ Drawdown trong ngày (`MAX_DAILY_DRAWDOWN_USD = 300.0`)**: Nếu tài khoản sụt giảm vượt quá $300 tính từ đỉnh cao nhất trong ngày, toàn bộ bot sẽ bị ngắt mạch mở lệnh mới.
2. **Bảo vệ Ký quỹ (`MIN_MARGIN_LEVEL_PERCENT = 200.0%`)**: Khi Margin Level xuống dưới 200%, hệ thống tự động khóa lệnh mới của tất cả các bot để tránh Margin Call / Stop Out.
3. **Giới hạn số lệnh đồng thời (`MAX_TOTAL_OPEN_POSITIONS = 10`)**: Giữ tổng số lệnh mở của cả 5 bot tối đa 10 vị thế để đảm bảo an toàn vốn.
