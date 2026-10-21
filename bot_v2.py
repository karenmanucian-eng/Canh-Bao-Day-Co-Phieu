"""BOT CỔ PHIẾU VÙNG ĐÁY - V2

ĐIỀU KIỆN BẮT BUỘC (cả 5 phải đạt ở phiên đã đóng gần nhất):
    RSI(14) < 30, RSI(7) < 30, MFI(14) < 20, MFI(11) < 20, khối lượng TB 10 phiên >= 20.000
Điều kiện tài chính bắt buộc (giữ như V1): 0 < P/E < 10, ROA > 0, ROE > 5%.

Hai điểm độc lập (đều là mô hình thử nghiệm, chưa backtest):
    Quality (0-100): định giá, sinh lời, tăng trưởng, an toàn tài chính, thanh khoản.
    Bottom  (0-100): mức quá bán (không thưởng RSI/MFI cực thấp), RSI/MFI quay đầu, giữ đáy,
                     trên MA5, MACD, lực cầu, phân kỳ dương.

Ba trạng thái giá: QUÁ BÁN - CHƯA XÁC NHẬN / ĐANG TẠO ĐÁY / XÁC NHẬN PHỤC HỒI SƠ BỘ.
Bốn nhóm kết quả: ĐỦ ĐIỀU KIỆN KỸ THUẬT / THEO DÕI VÙNG ĐÁY / TÀI CHÍNH YẾU / DỮ LIỆU CŨ.

Đây không phải lời khuyên đầu tư.

Chạy thử:  py -3.12 bot_v2.py --symbols STB DVM NO1 SCS --no-telegram
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import ta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # không bắt buộc khi chạy thử
    pass

import kbs_client

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
FILE_MA_LOI = "ma_loi.csv"

MUI_GIO_VN = timezone(timedelta(hours=7))
GIO_PHIEN_DONG = 15          # trước 15h thì phiên hôm nay chưa đóng -> bỏ nến hôm nay
SO_NGAY_LICH_SU = 170        # số ngày lịch tải về (đủ ~110 phiên)
NGUONG_KL_TB10 = 20000       # khối lượng TB 10 phiên tối thiểu
SO_PHIEN_THEO_DOI = 10       # giữ mã sau khi thoát quá bán tối đa N phiên (tham số thử nghiệm)
TOI_DA_TIN_CHI_TIET = 10     # mỗi nhóm gửi tối đa N tin chi tiết
TOI_DA_DONG_THEO_DOI = 15
RR_TOI_THIEU = 1.5


# ------------------------------------------------------------------ tiện ích

def tt(x, x0, x1):
    """Quy đổi tuyến tính về 0..1: x = x0 -> 0, x = x1 -> 1 (x0 có thể lớn hơn x1). None -> None."""
    if x is None:
        return None
    if x1 == x0:
        return 0.0
    return max(0.0, min(1.0, (x - x0) / (x1 - x0)))


def so(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def f(x, dinh_dang, don_vi=""):
    return "n/a" if x is None else f"{x:{dinh_dang}}{don_vi}"


def doc_ma_loi():
    if not os.path.exists(FILE_MA_LOI):
        return set()
    with open(FILE_MA_LOI, "r", encoding="utf-8") as fh:
        return set(line.strip() for line in fh if line.strip())


def ghi_ma_loi(ma):
    with open(FILE_MA_LOI, "a", encoding="utf-8") as fh:
        fh.write(ma + "\n")


def gui_telegram(noi_dung, khong_gui=False):
    """Gửi Telegram (tự chia nhỏ nếu quá dài). khong_gui=True thì chỉ in ra màn hình."""
    if khong_gui:
        print("\n" + noi_dung)
        return
    cac_doan, hien_tai = [], ""
    for dong in noi_dung.split("\n"):
        if len(hien_tai) + len(dong) + 1 > 3800:
            cac_doan.append(hien_tai)
            hien_tai = ""
        hien_tai += dong + "\n"
    if hien_tai.strip():
        cac_doan.append(hien_tai)
    for doan in cac_doan:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": doan.rstrip()}, timeout=15,
            )
            if r.status_code != 200:
                print(f"Telegram từ chối (mã {r.status_code}): {r.text}")
            else:
                print("Đã gửi Telegram OK")
        except Exception as e:  # noqa: BLE001
            print(f"Lỗi gửi Telegram: {e}")
        time.sleep(1)


def lay_tuy_chon(df_ratio, danh_sach_id, cot):
    """Giá trị chỉ số ở cột (quý) đã cho; None nếu KBS không có."""
    for id_can_tim in danh_sach_id:
        hang = df_ratio[df_ratio["item_id"] == id_can_tim]
        if not hang.empty:
            v = so(hang[cot].values[0])
            if v is not None:
                return v
    return None


def lay_chuoi(df_ratio, danh_sach_id):
    """Chuỗi giá trị các quý (mới nhất trước) của một chỉ số."""
    cot_quy = [c for c in df_ratio.columns if c not in ["item", "item_en", "item_id"]]
    for id_can_tim in danh_sach_id:
        hang = df_ratio[df_ratio["item_id"] == id_can_tim]
        if not hang.empty:
            return [so(hang[c].values[0]) for c in cot_quy]
    return []


# ------------------------------------------------------------------ dữ liệu giá & chỉ báo

def la_phien_sang(bay_gio):
    """Chạy trong giờ nghỉ trưa (từ 11:30 đến trước 13:00): nến hôm nay là kết quả phiên SÁNG."""
    phut = bay_gio.hour * 60 + bay_gio.minute
    return 11 * 60 + 30 <= phut < 13 * 60


def bo_phien_chua_dong(df, bay_gio):
    """Bỏ nến hôm nay nếu phiên chưa đóng (trước 15h), TRỪ khi đang chạy lúc nghỉ trưa: khi đó giữ
    nến hôm nay (kết quả phiên sáng) và tin nhắn sẽ ghi rõ là dữ liệu phiên sáng."""
    if la_phien_sang(bay_gio):
        return df
    if len(df) and bay_gio.hour < GIO_PHIEN_DONG and df["time"].iloc[-1].date() == bay_gio.date():
        return df.iloc[:-1].reset_index(drop=True)
    return df


def them_chi_bao(df):
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["RSI14"] = ta.momentum.RSIIndicator(close=c, window=14).rsi()
    df["RSI7"] = ta.momentum.RSIIndicator(close=c, window=7).rsi()
    df["MFI14"] = ta.volume.MFIIndicator(high=h, low=l, close=c, volume=v, window=14).money_flow_index()
    df["MFI11"] = ta.volume.MFIIndicator(high=h, low=l, close=c, volume=v, window=11).money_flow_index()
    df["ATR14"] = ta.volatility.AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range()
    df["MACDH"] = ta.trend.MACD(close=c).macd_diff()
    for n in (5, 10, 20, 50):
        df[f"MA{n}"] = c.rolling(n).mean()
    df["VOL20"] = v.rolling(20).mean()
    df["QUA_BAN"] = (df["RSI14"] < 30) & (df["RSI7"] < 30) & (df["MFI14"] < 20) & (df["MFI11"] < 20)
    return df


def phan_ky_duong(df):
    """Dấu hiệu phân kỳ dương: giá lập đáy thấp hơn/bằng nhưng RSI14 đáy sau cao hơn đáy trước."""
    if len(df) < 26:
        return False
    a, b = df.iloc[-10:], df.iloc[-25:-10]
    ia, ib = a["close"].idxmin(), b["close"].idxmin()
    ra, rb = df.at[ia, "RSI14"], df.at[ib, "RSI14"]
    if pd.isna(ra) or pd.isna(rb):
        return False
    return bool(df.at[ia, "close"] <= df.at[ib, "close"] * 1.01 and ra > rb + 2)


def tin_hieu_dao_chieu(df):
    n = len(df)
    r, p = df.iloc[-1], df.iloc[-2]
    th = {
        "rsi_tang": bool(r["RSI14"] > p["RSI14"]),
        "mfi_tang": bool(r["MFI14"] > p["MFI14"]),
        "giu_day": bool(df["low"].iloc[-3:].min() >= df["low"].iloc[-13:-3].min()) if n >= 14 else False,
        "tren_ma5": bool(pd.notna(r["MA5"]) and r["close"] > r["MA5"]),
        "macd": bool(pd.notna(r["MACDH"]) and pd.notna(p["MACDH"]) and r["MACDH"] > p["MACDH"]),
        "luc_cau": bool(pd.notna(r["VOL20"]) and r["close"] > r["open"] and r["volume"] >= r["VOL20"]),
        "phan_ky": phan_ky_duong(df),
    }
    th["n_core"] = sum(th[k] for k in ("rsi_tang", "mfi_tang", "giu_day", "tren_ma5"))
    if th["n_core"] == 4:
        th["trang_thai"] = "XÁC NHẬN PHỤC HỒI SƠ BỘ"
    elif th["n_core"] >= 2:
        th["trang_thai"] = "ĐANG TẠO ĐÁY"
    else:
        th["trang_thai"] = "QUÁ BÁN - CHƯA XÁC NHẬN"
    return th


def ho_tro_khang_cu(df):
    """Hỗ trợ/kháng cự theo đỉnh-đáy cục bộ (cửa sổ 3 phiên mỗi bên) trong ~70 phiên.
    Kháng cự chỉ hợp lệ nếu nằm trong +2% đến +35% so với giá. Không có thì để None, không tự bịa mục tiêu."""
    gia = float(df["close"].iloc[-1])
    atr = float(df["ATR14"].iloc[-1])
    w = df.iloc[-70:].reset_index(drop=True)
    k = 3
    dinh, day = [], []
    for i in range(k, len(w) - k):
        if w["high"].iloc[i] == w["high"].iloc[i - k:i + k + 1].max():
            dinh.append(float(w["high"].iloc[i]))
        if w["low"].iloc[i] == w["low"].iloc[i - k:i + k + 1].min():
            day.append(float(w["low"].iloc[i]))
    kc = sorted(set(round(x, 2) for x in dinh if gia * 1.02 < x <= gia * 1.35))
    r1 = kc[0] if kc else None
    r2 = next((x for x in kc if r1 and x > r1 * 1.03), None)

    ht = [x for x in day if gia * 0.85 <= x < gia * 0.995]
    if ht:
        s = max(ht)
    else:
        day20 = float(df["low"].iloc[-20:].min())
        s = day20 if day20 < gia * 0.995 else gia - atr
    stop = min(s - 0.5 * atr, gia * 0.995)
    rui_ro = gia - stop
    rr1 = (r1 - gia) / rui_ro if (r1 and rui_ro > 0) else None
    rr2 = (r2 - gia) / rui_ro if (r2 and rui_ro > 0) else None
    return {
        "gia": gia, "atr": atr, "atr_pct": atr / gia * 100, "ho_tro": s, "stop": stop,
        "rui_ro_pct": rui_ro / gia * 100, "kc1": r1, "kc2": r2, "rr1": rr1, "rr2": rr2,
        "vung_mua_tu": max(s, gia - atr), "vung_mua_den": gia,
    }


def diem_bottom(r):
    """Bottom Score 0-100. Mức quá bán có 'vùng phẳng': RSI14 12-30 và MFI14 5-20 được điểm tối đa,
    thấp hơn nữa bị GIẢM điểm (cảnh báo rơi tự do), không thưởng cực thấp."""
    rsi_pt = 1.0 if r["rsi14"] >= 12 else tt(r["rsi14"], 2, 12)
    mfi_pt = 1.0 if r["mfi14"] >= 5 else tt(r["mfi14"], 0, 5)
    th = r["th"]
    cac_phan = {
        "Mức quá bán (không thưởng cực thấp)": (20, (rsi_pt + mfi_pt) / 2),
        "RSI quay đầu": (15, 1.0 if th["rsi_tang"] else 0.0),
        "MFI quay đầu": (10, 1.0 if th["mfi_tang"] else 0.0),
        "Giữ đáy 3 phiên": (15, 1.0 if th["giu_day"] else 0.0),
        "Trên MA5": (10, 1.0 if th["tren_ma5"] else 0.0),
        "MACD cải thiện": (10, 1.0 if th["macd"] else 0.0),
        "Lực cầu": (10, 1.0 if th["luc_cau"] else 0.0),
        "Phân kỳ dương": (10, 1.0 if th["phan_ky"] else 0.0),
    }
    return sum(m * v for m, v in cac_phan.values())


# ------------------------------------------------------------------ tài chính

def phan_tich_tai_chinh(chi_so, gia, vol_tb10, atr_pct):
    """Trả về dict chỉ số + Quality + cảnh báo + trạng thái tài chính. chi_so có thể None (lỗi tải)."""
    tc = {"co_du_lieu": chi_so is not None and not chi_so.empty}
    if tc["co_du_lieu"]:
        cot = [c for c in chi_so.columns if c not in ["item", "item_en", "item_id"]][0]
        eps = lay_tuy_chon(chi_so, ["trailing_eps"], cot)
        roa = lay_tuy_chon(chi_so, ["roa_trailling", "roa"], cot)
        roe = lay_tuy_chon(chi_so, ["roe_trailling", "roe"], cot)
        bvps_kbs = lay_tuy_chon(chi_so, ["book_value_per_share_bvps", "book_value_per_share", "bvps"], cot)
        cr = lay_tuy_chon(chi_so, ["current_ratio", "short_term_ratio"], cot)
        if cr is not None and cr > 20:
            cr /= 100
        lte = lay_tuy_chon(chi_so, ["liabilities_to_equity"], cot)
        tc["gm"] = lay_tuy_chon(chi_so, ["gross_profit_margin", "gross_margin"], cot)
        tc["nm"] = lay_tuy_chon(chi_so, ["net_profit_margin", "net_margin"], cot)
        chuoi_eps = lay_chuoi(chi_so, ["trailing_eps"])
    else:
        eps = roa = roe = bvps_kbs = cr = lte = None
        tc["gm"] = tc["nm"] = None
        chuoi_eps = []

    tc.update({"eps": eps, "roa": roa, "roe": roe, "cr": cr, "bvps_kbs": bvps_kbs})
    tc["pe"] = gia * 1000 / eps if eps and eps > 0 else None
    bvps = bvps_kbs if bvps_kbs and bvps_kbs > 0 else (eps / (roe / 100) if eps and roe and roe > 0 else None)
    tc["bvps"] = bvps
    tc["pb"] = gia * 1000 / bvps if bvps and bvps > 0 else None
    tc["don_bay"] = roe / roa if roa and roa > 0 and roe is not None else None
    tc["de"] = lte / 100 if lte is not None else (tc["don_bay"] - 1 if tc["don_bay"] else None)
    tc["la_tctc"] = bool((tc["don_bay"] and tc["don_bay"] > 8) or (tc["de"] is not None and tc["de"] > 8))
    tc["roe_chk"] = eps / bvps_kbs * 100 if bvps_kbs and bvps_kbs > 0 and eps else None

    tc["eps_yoy"] = None
    if len(chuoi_eps) >= 5 and chuoi_eps[0] is not None and chuoi_eps[4] and chuoi_eps[4] > 0:
        tc["eps_yoy"] = (chuoi_eps[0] - chuoi_eps[4]) / chuoi_eps[4] * 100
    tc["eps_xh"] = None
    cuoi = [x for x in chuoi_eps[:4] if x is not None]
    if len(cuoi) >= 4:
        tc["eps_xh"] = sum(1 for k in range(3) if cuoi[k] > cuoi[k + 1])

    # ---------- Quality: nhóm -> (điểm đạt, điểm tối đa khả dụng); chỉ số thiếu thì bỏ khỏi mẫu số
    def nhom(*tp):
        return (sum(m * v for m, v in tp if v is not None), sum(m for m, v in tp if v is not None))

    if tc["pe"] is not None:
        v_pe = tt(tc["pe"], 20, 8)
    else:
        v_pe = 0.0 if (tc["co_du_lieu"] and eps is not None) else None   # lỗ: 0 điểm, không loại khỏi mẫu số
    v_pb = None
    if tc["pb"] is not None:
        v_pb = tt(tc["pb"], 3, 1)
        if tc["pb"] < 0.3:
            v_pb *= 0.5
    roa_cao = 1.5 if tc["la_tctc"] else 5
    nhom_q = {
        "Định giá": (nhom((10, v_pe), (10, v_pb)), 20),
        "Sinh lời": (nhom((10, tt(roe, 0, 15)), (8, tt(roa, 0, roa_cao)),
                          (7, tt(tc["nm"], 0, 10))), 25),
        "Tăng trưởng": (nhom((14, tt(tc["eps_yoy"], -20, 20)),
                             (6, tc["eps_xh"] / 3 if tc["eps_xh"] is not None else None)), 20),
        "An toàn tài chính": (nhom((10, None if tc["la_tctc"] else tt(tc["de"], 2.5, 0.5)),
                                   (10, None if tc["la_tctc"] else tt(cr, 0.8, 1.5))), 20),
        "Thanh khoản": (nhom((15, tt(vol_tb10, 20000, 500000))), 15),
    }
    dat = sum(v[0][0] for v in nhom_q.values())
    kha_dung = sum(v[0][1] for v in nhom_q.values())
    ap_dung = 100 - (20 if tc["la_tctc"] else 0)
    tc["phu_du_lieu"] = kha_dung / ap_dung if ap_dung else 0.0
    quality_goc = dat / kha_dung * 100 if kha_dung else 0.0

    # ---------- cảnh báo
    yeu, nang, nhe, ghi_chu = [], [], [], []
    if tc["co_du_lieu"]:
        if eps is not None and eps <= 0:
            yeu.append(f"EPS 4 quý âm ({eps:,.0f}đ): doanh nghiệp đang lỗ")
        if roe is not None and roe < 0:
            yeu.append(f"ROE âm ({roe:.1f}%)")
        if roa is not None and roa < 0:
            yeu.append(f"ROA âm ({roa:.1f}%)")
        if bvps is not None and bvps <= 0:
            yeu.append("giá trị sổ sách âm (vốn chủ sở hữu âm)")
        if tc["pe"] is not None and tc["pe"] < 2:
            nang.append(f"P/E {tc['pe']:.1f} < 2 (có thể do lãi bất thường một lần hoặc dữ liệu sai)")
        if roe is not None and roe > 60:
            nang.append(f"ROE {roe:.0f}% > 60% (bất thường, nên đối chiếu báo cáo tài chính)")
        if tc["eps_yoy"] is not None and tc["eps_yoy"] < -30:
            nang.append(f"EPS 4 quý giảm {abs(tc['eps_yoy']):.0f}% so với cùng kỳ năm trước")
        if tc["eps_yoy"] is not None and tc["eps_yoy"] > 300:
            nhe.append(f"EPS 4 quý tăng {tc['eps_yoy']:.0f}% so với cùng kỳ (có thể do lãi một lần)")
        if tc["pb"] is not None and tc["pb"] < 0.3:
            nhe.append(f"P/B {tc['pb']:.2f} < 0.3 (giá thấp xa giá trị sổ sách, nên kiểm tra tài sản/nợ)")
        if roa and roa > 0 and roe is not None and roe / roa > 20:
            ghi = f"ROE/ROA = {roe / roa:.0f} lần (đòn bẩy cao hoặc số liệu cần kiểm tra)"
            (ghi_chu if tc["la_tctc"] else nhe).append(ghi)
        rc = tc["roe_chk"]
        if rc is not None and rc > 0 and roe is not None and roe > 0 and (roe / rc > 1.6 or roe / rc < 0.6):
            ghi = f"ROE KBS {roe:.1f}% lệch nhiều so với EPS/giá trị sổ sách ({rc:.1f}%): nên đối chiếu báo cáo tài chính"
            (ghi_chu if tc["la_tctc"] else nhe).append(ghi)
        if not tc["la_tctc"]:
            if tc["de"] is not None and tc["de"] > 3:
                nhe.append(f"Nợ/Vốn chủ ~ {tc['de']:.1f} lần > 3 (đòn bẩy cao)")
            if cr is not None and cr < 1:
                nhe.append(f"thanh toán hiện hành {cr:.2f} < 1")
        else:
            ghi_chu.append("tổ chức tài chính (đòn bẩy cao): chưa có NPL/CAR/NIM nên chưa thẩm định được an toàn")
    else:
        nhe.append("không tải được chỉ số tài chính")
    if gia < 5:
        nang.append(f"giá {gia * 1000:,.0f}đ < 5.000đ (cổ phiếu penny, rủi ro cao)")
    if atr_pct > 10:
        nang.append(f"biến động {atr_pct:.1f}%/phiên > 10% (rủi ro bắt dao rơi)")
    elif atr_pct > 8:
        nhe.append(f"biến động {atr_pct:.1f}%/phiên > 8%")
    if vol_tb10 < 50000:
        nhe.append(f"khối lượng TB 10 phiên {vol_tb10:,.0f} < 50.000 (thanh khoản mỏng, khó thoát lệnh)")
    if tc["phu_du_lieu"] < 0.7:
        nhe.append(f"chỉ có {tc['phu_du_lieu'] * 100:.0f}% chỉ số tài chính áp dụng được")

    tru = min(45, 15 * len(nang) + 7 * len(nhe))
    tc["quality"] = max(0.0, quality_goc - tru)
    tc["quality_goc"] = quality_goc
    tc["tru"] = tru
    tc["nhom_q"] = {k: (v[0][0] / v[0][1] * v[1] if v[0][1] else None, v[1]) for k, v in nhom_q.items()}
    tc["tin_cay"] = max(0.0, tc["phu_du_lieu"] * 100 - 12 * (len(nang) + len(yeu)) - 5 * len(nhe))
    tc["yeu"], tc["nang"], tc["nhe"], tc["ghi_chu"] = yeu, nang, nhe, ghi_chu

    if yeu or nang:
        tc["trang_thai"] = "🔴 Tài chính yếu/bất thường"
    elif tc["la_tctc"] or nhe or tc["quality"] < 55 or not tc["co_du_lieu"]:
        tc["trang_thai"] = "🟡 Cần thẩm định"
    else:
        tc["trang_thai"] = "🟢 Đạt chuẩn tài chính sơ bộ"
    return tc


# ------------------------------------------------------------------ phân tích một mã

def phan_tich_gia(df):
    """Phân tích kỹ thuật cho một DataFrame giá đã có chỉ báo. Trả None nếu thiếu dữ liệu."""
    if len(df) < 30:
        return None
    r = df.iloc[-1]
    if r[["RSI14", "RSI7", "MFI14", "MFI11", "ATR14"]].isna().any():
        return None
    vol_tb10 = float(df["volume"].tail(10).mean())
    pc20 = df["close"].pct_change().tail(20).abs()
    kq = {
        "ngay": r["time"].date(), "gia": float(r["close"]),
        "rsi14": float(r["RSI14"]), "rsi7": float(r["RSI7"]),
        "mfi14": float(r["MFI14"]), "mfi11": float(r["MFI11"]),
        "vol_tb10": vol_tb10, "qua_ban_4": bool(r["QUA_BAN"]), "du_kl": vol_tb10 >= NGUONG_KL_TB10,
        "macdh_tang": bool(pd.notna(r["MACDH"]) and r["MACDH"] > df["MACDH"].iloc[-2]),
        "so_phien_bien_dong_10": int((pc20 >= 0.10).sum()),
        "duoi_ma50": bool(pd.notna(r["MA50"]) and r["close"] < r["MA50"]),
        "duoi_ma20": bool(pd.notna(r["MA20"]) and r["close"] < r["MA20"]),
    }
    kq["bat_buoc"] = kq["qua_ban_4"] and kq["du_kl"]
    kq["th"] = tin_hieu_dao_chieu(df)
    kq["kich_ban"] = ho_tro_khang_cu(df)
    kq["bottom"] = diem_bottom(kq)

    # đã thoát quá bán trong vòng SO_PHIEN_THEO_DOI phiên gần đây (không tính phiên hôm nay)?
    kq["day20"] = float(df["low"].iloc[-20:].min())
    kq["tang_tu_day20"] = (kq["gia"] / kq["day20"] - 1) * 100
    kq["thoat_qua_ban"] = None
    kq["day_qua_ban"] = None
    kq["tang_tu_day"] = None
    if not kq["qua_ban_4"] and kq["du_kl"]:
        gan = df.iloc[-(SO_PHIEN_THEO_DOI + 1):-1]
        co = gan[gan["QUA_BAN"]]
        if len(co):
            j = co.index[-1]
            so_phien = len(df) - 1 - j
            day_j = float(df["low"].iloc[max(0, j - 2):j + 1].min())
            if r["close"] >= day_j and r["RSI14"] <= 50:
                kq["thoat_qua_ban"] = so_phien
                kq["day_qua_ban"] = float(df["low"].iloc[max(0, j - 2):].min())
                kq["tang_tu_day"] = (kq["gia"] / kq["day_qua_ban"] - 1) * 100
    return kq


def phan_nhom(r, ngay_tham_chieu):
    if r["ngay"] < ngay_tham_chieu:
        return "DU_LIEU_CU"
    tc = r["tc"]
    if tc is None:
        return "THEO_DOI"
    if tc["trang_thai"].startswith("🔴"):
        return "TAI_CHINH_YEU"
    rr1 = r["kich_ban"]["rr1"]
    if (r.get("khong_dat_bat_buoc") is not True and r["th"]["trang_thai"] == "XÁC NHẬN PHỤC HỒI SƠ BỘ" and tc["trang_thai"].startswith("🟢")
            and rr1 is not None and rr1 >= RR_TOI_THIEU):
        return "DU_DIEU_KIEN"
    return "THEO_DOI"


def khoa_xep_hang(r):
    q = r["tc"]["quality"] if r["tc"] else 0.0
    return 0.6 * q + 0.4 * r["bottom"]   # giả thuyết 60/40, chỉ dùng để sắp thứ tự trong nhóm


# ------------------------------------------------------------------ tin nhắn

def nhan_muc_do(r):
    """Nhãn mức độ: 🟢 đáng nghiên cứu trước / 🟡 cần xem kỹ / 🔴 rủi ro rất cao."""
    tc, th, kb = r["tc"], r["th"], r["kich_ban"]
    if (tc["trang_thai"].startswith("🔴") or r["gia"] < 5 or kb["atr_pct"] > 10
            or r["rsi14"] < 8 or r["mfi14"] < 2):
        return "🔴", "RỦI RO RẤT CAO"
    if tc["quality"] >= 70 and th["n_core"] >= 2 and not tc["nang"]:
        return "🟢", "Đáng nghiên cứu trước"
    return "🟡", "Cần xem kỹ"


def ket_luan(r):
    """Kết luận theo quy tắc (KHÔNG phải lời khuyên cá nhân): các dòng gạch đầu dòng."""
    tc, th, kb = r["tc"], r["th"], r["kich_ban"]
    icon, _ = nhan_muc_do(r)
    vung = f"{kb['vung_mua_tu']:.2f}–{kb['vung_mua_den']:.2f}"
    kich_ban = f"vùng {vung}, cắt lỗ {kb['stop']:.2f} (-{kb['rui_ro_pct']:.1f}%)"
    if kb["kc1"]:
        kich_ban += f", mục tiêu {kb['kc1']:.2f} (+{(kb['kc1'] / kb['gia'] - 1) * 100:.1f}%)"
    if icon == "🔴":
        ly_do = (tc["yeu"] + tc["nang"] + [c for c in ("giá penny dưới 5.000đ" if r["gia"] < 5 else "",
                 "RSI/MFI cực thấp, đà giảm có thể còn tiếp" if (r["rsi14"] < 8 or r["mfi14"] < 2) else "") if c])
        return ["👉 Theo quy tắc: KHÔNG thuộc diện cân nhắc (rủi ro rất cao)",
                f"• Lý do: {ly_do[0] if ly_do else 'rủi ro cao'}",
                "• Chỉ theo dõi, không có vùng giá khuyến nghị"]
    thieu = [ten for ten, k in (("RSI tăng", "rsi_tang"), ("MFI tăng", "mfi_tang"),
             ("giữ đáy 3 phiên", "giu_day"), ("giá trên MA5", "tren_ma5")) if not th[k]]
    luu_y = "; chất lượng/tài chính cần thẩm định thêm" if icon == "🟡" else ""
    if th["n_core"] == 4:
        kl = [f"👉 Theo quy tắc: CÓ THỂ CÂN NHẮC vào thăm dò: {kich_ban}{luu_y}"]
        if kb["rr1"] is not None and kb["rr1"] < RR_TOI_THIEU:
            kl.append(f"• R:R {kb['rr1']:.1f} thấp (<{RR_TOI_THIEU}): chờ giá về gần hỗ trợ {kb['ho_tro']:.2f} để R:R tốt hơn")
        return kl
    if th["n_core"] >= 2:
        return [f"👉 Theo quy tắc: CHỜ XÁC NHẬN (còn thiếu: {', '.join(thieu)}){luu_y}",
                f"• Nếu cân nhắc khi đủ tín hiệu: {kich_ban}",
                f"• Hủy kịch bản nếu giá đóng cửa dưới {kb['stop']:.2f}"]
    return ["👉 Theo quy tắc: CHƯA NÊN VÀO (giá chưa quay đầu)",
            f"• Chờ: RSI14 tăng, MFI14 tăng, giá trên MA5. Vùng quan sát {vung}",
            f"• Nếu thủng {kb['stop']:.2f} thì kịch bản đáy không còn"]


def tin_gon(r, thu_tu):
    tc, th, kb = r["tc"], r["th"], r["kich_ban"]
    icon, nhan = nhan_muc_do(r)
    dong = [f"{thu_tu}) {r['ma']} · {r['gia']:.2f} nghìn · {icon} {nhan}",
            f"• Điểm chất lượng (tài chính): {tc['quality']:.0f}/100",
            f"• Điểm tín hiệu đáy (kỹ thuật): {r['bottom']:.0f}/100 · Tin cậy dữ liệu: {tc['tin_cay']:.0f}/100",
            f"• Giá: {th['trang_thai'].lower()} ({th['n_core']}/4 tín hiệu quay đầu)",
            f"• Quá bán: RSI14 {r['rsi14']:.0f} · MFI14 {r['mfi14']:.0f}",
            f"• Tài chính: P/E {f(tc['pe'], '.1f')} · ROE {f(tc['roe'], '.1f', '%')} · ROA {f(tc['roa'], '.1f', '%')}",
            f"• Đáy 20 phiên {r['day20']:.2f} → giá hiện tại cao hơn {r['tang_tu_day20']:+.1f}%",
            f"• Vùng giá {kb['vung_mua_tu']:.2f}–{kb['vung_mua_den']:.2f} · Cắt lỗ {kb['stop']:.2f} (-{kb['rui_ro_pct']:.1f}%)"]
    if kb["kc1"]:
        dong.append(f"• Mục tiêu {kb['kc1']:.2f} (+{(kb['kc1'] / kb['gia'] - 1) * 100:.1f}%) · R:R {kb['rr1']:.1f}")
    else:
        dong.append("• Mục tiêu: chưa có kháng cự gần")
    dong[1:1] = ket_luan(r)
    canh_bao = []
    if r["rsi14"] < 8 or r["mfi14"] < 2:
        canh_bao.append("RSI/MFI cực thấp, đà giảm có thể còn tiếp")
    if r["duoi_ma50"]:
        canh_bao.append("Giá dưới MA50")
    if tc["la_tctc"]:
        canh_bao.append("Tổ chức tài chính: chưa kiểm tra NPL/CAR/NIM")
    if r.get("so_phien_gd_20") is not None and r["so_phien_gd_20"] < 15:
        canh_bao.append(f"Chỉ {r['so_phien_gd_20']}/20 phiên có giao dịch")
    canh_bao += tc["yeu"] + tc["nang"] + tc["nhe"]
    for c in canh_bao[:3]:
        dong.append(f"• ⚠️ {c}")
    if len(canh_bao) > 3:
        dong.append(f"• ⚠️ +{len(canh_bao) - 3} cảnh báo khác")
    if r.get("khong_dat_bat_buoc"):
        dong.append("• ⛔ Không đạt điều kiện bắt buộc (chỉ xem thử)")
    return "\n".join(dong)


def tin_dau(bay_gio, ngay, danh_sach, thu):
    ma = ", ".join(r["ma"] for r in danh_sach) if danh_sach else "không có mã nào"
    if la_phien_sang(bay_gio) and ngay == bay_gio.date():
        nguon = f"kết quả PHIÊN SÁNG {ngay.strftime('%d/%m/%Y')}, chưa đóng cửa, có thể đổi ở phiên chiều"
    else:
        nguon = f"dữ liệu phiên đã đóng {ngay.strftime('%d/%m/%Y')}"
    return (f"📉 CẢNH BÁO VÙNG ĐÁY" + (" (CHẠY THỬ)" if thu else "") + "\n"
            f"🕒 Quét lúc {bay_gio.strftime('%H:%M %d/%m/%Y')} ({nguon})\n"
            f"✅ Mã đạt đủ điều kiện ({len(danh_sach)}): {ma}")


def dong_sau_qua_ban(ds):
    dong = [f"🔁 VỪA THOÁT QUÁ BÁN (trong {SO_PHIEN_THEO_DOI} phiên gần đây, giá chưa thủng đáy)",
            "Chất lượng = điểm tài chính (0-100). Tín hiệu đáy = điểm kỹ thuật quay đầu (0-100). "
            "Lời/lỗ = giá hiện tại so với giá thấp nhất của đợt quá bán."]
    for r in ds[:TOI_DA_DONG_THEO_DOI]:
        tc = r["tc"]
        icon = nhan_muc_do(r)[0]
        dong.append(f"• {icon} {r['ma']} · giá {r['gia']:.2f} · đáy {r['day_qua_ban']:.2f} · "
                    f"{r['tang_tu_day']:+.1f}% so với đáy · thoát quá bán {r['thoat_qua_ban']} phiên trước · "
                    f"Chất lượng {tc['quality']:.0f} · Tín hiệu đáy {r['bottom']:.0f} · RSI14 {r['rsi14']:.0f}")
    if len(ds) > TOI_DA_DONG_THEO_DOI:
        dong.append(f"... và {len(ds) - TOI_DA_DONG_THEO_DOI} mã nữa")
    return "\n".join(dong)


CHAN_TRANG = "ℹ️ Kết luận là kết quả đối chiếu quy tắc của bot, mô hình chưa backtest, không phải lời khuyên đầu tư cá nhân."


def dat_dieu_kien_tai_chinh(tc):
    """0 < P/E < 10, ROA > 0, ROE > 5%. Thiếu dữ liệu thì coi là KHÔNG đạt (không kiểm chứng được)."""
    pe, roa, roe = tc["pe"], tc["roa"], tc["roe"]
    return bool(pe is not None and 0 < pe < 10 and roa is not None and roa > 0 and roe is not None and roe > 5)


def phan_tich_ma(ma, df, lay_chi_so, can_tai_chinh_neu_khong_dat=False):
    """Phân tích đầy đủ 1 mã từ DataFrame giá (đã bỏ nến chưa đóng, đã lọc volume>0).
    lay_chi_so: hàm (ma) -> DataFrame chỉ số tài chính. Trả (kết quả hoặc None, lỗi_tài_chính: bool)."""
    df = them_chi_bao(df)
    kq = phan_tich_gia(df)
    if kq is None:
        return None, False
    kq["ma"] = ma
    kq["tc"] = None
    loi_tc = False
    can = kq["bat_buoc"] or kq["thoat_qua_ban"] is not None or can_tai_chinh_neu_khong_dat
    if can:
        try:
            chi_so = lay_chi_so(ma)
        except Exception as e:  # noqa: BLE001
            print(f"  -> Không tải được tài chính {ma}: {e}")
            chi_so, loi_tc = None, True
        kq["tc"] = phan_tich_tai_chinh(chi_so, kq["gia"], kq["vol_tb10"], kq["kich_ban"]["atr_pct"])
        kq["tai_chinh_dat"] = dat_dieu_kien_tai_chinh(kq["tc"])
        kq["ky_thuat_dat"] = kq["bat_buoc"]
        kq["bat_buoc"] = kq["bat_buoc"] and kq["tai_chinh_dat"]
    return kq, loi_tc


def chay(args):
    bay_gio = datetime.now(MUI_GIO_VN)
    ngay_ket_thuc = bay_gio.strftime("%Y-%m-%d")
    ngay_bat_dau = (bay_gio - timedelta(days=SO_NGAY_LICH_SU)).strftime("%Y-%m-%d")
    che_do_thu = bool(args.symbols)
    if args.gio:  # chỉ để thử: giả lập giờ chạy, ví dụ --gio 11:31
        h, m = (int(x) for x in args.gio.split(":"))
        bay_gio = bay_gio.replace(hour=h, minute=m)

    ds_goc = [s.upper() for s in args.symbols] if che_do_thu else kbs_client.danh_sach_co_phieu()
    ma_loi_cu = set() if che_do_thu else doc_ma_loi()
    ds = [m for m in ds_goc if m not in ma_loi_cu]
    print(f"Thời điểm chạy (giờ VN): {bay_gio:%Y-%m-%d %H:%M} | tổng {len(ds_goc)} mã, "
          f"loại {len(ma_loi_cu)} mã không có dữ liệu cũ, quét {len(ds)}")

    ket_qua, loi_api, loi_tai_chinh, khong_du_lieu, ngay_cuoi_tat_ca = [], [], [], [], []
    loai_tai_chinh = []
    for i, ma in enumerate(ds):
        print(f"[{i + 1}/{len(ds)}] {ma}")
        try:
            df = kbs_client.lich_su_gia(ma, ngay_bat_dau, ngay_ket_thuc)
            if df is not None:
                df = bo_phien_chua_dong(df, bay_gio)
                so_phien_gd_20 = int((df["volume"].tail(20) > 0).sum())
                df = df[df["volume"] > 0].reset_index(drop=True)
            if df is None or len(df) < 30:
                khong_du_lieu.append(ma)
                if not che_do_thu:
                    ghi_ma_loi(ma)
                continue
            ngay_cuoi_tat_ca.append(df["time"].iloc[-1].date())
            kq, loi_tc = phan_tich_ma(
                ma, df, lambda m: kbs_client.chi_so_tai_chinh(m, limit=5),
                can_tai_chinh_neu_khong_dat=args.show_all or che_do_thu,
            )
            if kq is not None:
                kq["so_phien_gd_20"] = so_phien_gd_20
            if loi_tc:
                loi_tai_chinh.append(ma)
            if kq is None:
                khong_du_lieu.append(ma)
                continue
            if kq["bat_buoc"]:
                print(f"  -> đạt điều kiện bắt buộc: {ma}")
            if kq["bat_buoc"] or kq["thoat_qua_ban"] is not None or kq["tc"] is not None:
                kq["khong_dat_bat_buoc"] = not kq["bat_buoc"] and kq["tc"] is not None and che_do_thu
                if kq["ky_thuat_dat"] if "ky_thuat_dat" in kq else False:
                    if not kq["tai_chinh_dat"]:
                        print(f"  -> đạt kỹ thuật nhưng KHÔNG đạt P/E/ROA/ROE: {ma}")
                        loai_tai_chinh.append(ma)
                ket_qua.append(kq)
        except Exception as e:  # noqa: BLE001  (lỗi mạng/API tạm thời KHÔNG ghi vào ma_loi.csv)
            print(f"  -> Lỗi với {ma}: {e}")
            loi_api.append(ma)
        time.sleep(0.8 if che_do_thu else 2.5)

    if not ngay_cuoi_tat_ca:
        gui_telegram("⚠️ BOT VÙNG ĐÁY V2: không lấy được dữ liệu giá nào, cần kiểm tra kết nối.", args.no_telegram)
        return
    ngay_tham_chieu = max(ngay_cuoi_tat_ca)

    nhom = {k: [] for k in ("DU_DIEU_KIEN", "THEO_DOI", "TAI_CHINH_YEU", "DU_LIEU_CU", "SAU_QUA_BAN")}
    for r in ket_qua:
        if r["bat_buoc"] or r.get("khong_dat_bat_buoc"):
            nhom[phan_nhom(r, ngay_tham_chieu)].append(r)
        elif (r["thoat_qua_ban"] is not None and r["ngay"] >= ngay_tham_chieu
              and r["tc"] is not None and r.get("tai_chinh_dat") and not r["tc"]["trang_thai"].startswith("🔴")):
            nhom["SAU_QUA_BAN"].append(r)
    for k in ("DU_DIEU_KIEN", "THEO_DOI", "TAI_CHINH_YEU"):
        nhom[k].sort(key=khoa_xep_hang, reverse=True)
    nhom["SAU_QUA_BAN"].sort(key=khoa_xep_hang, reverse=True)

    danh_sach = nhom["DU_DIEU_KIEN"] + nhom["THEO_DOI"] + nhom["TAI_CHINH_YEU"]
    thu_tu_nhan = {"🟢": 0, "🟡": 1, "🔴": 2}
    danh_sach.sort(key=lambda r: (thu_tu_nhan[nhan_muc_do(r)[0]], -khoa_xep_hang(r)))

    khoi = [tin_dau(bay_gio, ngay_tham_chieu, danh_sach, che_do_thu)]
    if not danh_sach:
        khoi.append("Hôm nay không có mã nào đạt đủ điều kiện.")
    for thu_tu, r in enumerate(danh_sach[:TOI_DA_TIN_CHI_TIET * 2], start=1):
        khoi.append(tin_gon(r, thu_tu))
    if len(danh_sach) > TOI_DA_TIN_CHI_TIET * 2:
        khoi.append("... còn: " + ", ".join(r["ma"] for r in danh_sach[TOI_DA_TIN_CHI_TIET * 2:]))
    if nhom["SAU_QUA_BAN"]:
        khoi.append(dong_sau_qua_ban(nhom["SAU_QUA_BAN"]))
    ghi = []
    if loai_tai_chinh:
        ghi.append("Trượt P/E/ROA/ROE: " + ", ".join(loai_tai_chinh[:20]))
    if nhom["DU_LIEU_CU"]:
        ghi.append("Dữ liệu cũ (không xếp hạng): " + ", ".join(
            f"{r['ma']} (phiên cuối {r['ngay'].strftime('%d/%m')})" for r in nhom["DU_LIEU_CU"]))
    if loi_api:
        ghi.append(f"Lỗi tải giá {len(loi_api)} mã: " + ", ".join(loi_api[:15]))
    if loi_tai_chinh:
        ghi.append("Không tải được tài chính: " + ", ".join(loi_tai_chinh[:15]))
    if ghi:
        khoi.append("\n".join("• " + g for g in ghi))
    khoi.append(CHAN_TRANG)

    tin, hien = [], ""
    for k in khoi:
        if hien and len(hien) + len(k) + 2 > 3500:
            tin.append(hien)
            hien = ""
        hien += ("\n\n" if hien else "") + k
    if hien:
        tin.append(hien)
    for t in tin:
        gui_telegram(t, args.no_telegram)
    print("\nHoàn tất!")


def doc_tham_so(argv=None):
    p = argparse.ArgumentParser(description="BOT cổ phiếu vùng đáy V2")
    p.add_argument("--symbols", nargs="*", help="chỉ quét các mã này (chế độ thử, hiển thị cả mã không đạt điều kiện)")
    p.add_argument("--no-telegram", action="store_true", help="không gửi Telegram, chỉ in ra màn hình")
    p.add_argument("--gio", help="chỉ để thử: giả lập giờ chạy theo giờ VN, ví dụ 11:31 hoặc 06:00")
    p.add_argument("--show-all", action="store_true", help="tải tài chính và hiển thị cả mã không đạt điều kiện bắt buộc")
    return p.parse_args(argv)


if __name__ == "__main__":
    chay(doc_tham_so())
