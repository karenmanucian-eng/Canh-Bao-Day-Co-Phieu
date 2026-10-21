import ta
import time
import requests
import csv
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

import kbs_client

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
FILE_MA_LOI = "ma_loi.csv"

ngay_hom_nay = datetime.now().strftime("%Y-%m-%d")
ngay_bat_dau = (datetime.now() - timedelta(days=150)).strftime("%Y-%m-%d")


def gui_telegram(noi_dung):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": noi_dung}
    try:
        r = requests.post(url, data=data, timeout=15)
        if r.status_code != 200:
            print(f"Telegram từ chối (mã {r.status_code}): {r.text}")
        else:
            print("Đã gửi Telegram OK")
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")


def doc_ma_loi():
    if not os.path.exists(FILE_MA_LOI):
        return set()
    with open(FILE_MA_LOI, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())


def ghi_ma_loi(ma):
    with open(FILE_MA_LOI, 'a', encoding='utf-8') as f:
        f.write(ma + "\n")


def lay_gia_tri(df_ratio, danh_sach_id_uu_tien, cot):
    for id_can_tim in danh_sach_id_uu_tien:
        hang = df_ratio[df_ratio['item_id'] == id_can_tim]
        if not hang.empty:
            return hang[cot].values[0]
    raise ValueError(f"Không tìm thấy chỉ số trong {danh_sach_id_uu_tien}")


def phan_tich_ma(df, roa, roe, pe, eps_ttm, vol_tb_10):
    """Phân tích kỹ thuật tự động theo công thức cố định (giá tính bằng nghìn đồng)."""
    gia = df['close'].iloc[-1]
    atr = ta.volatility.AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'], window=14
    ).average_true_range().iloc[-1]
    day_20 = df['low'].tail(20).min()
    dinh_60 = df['high'].tail(60).max()
    ma20 = df['close'].tail(20).mean()
    ma50 = df['close'].tail(50).mean() if len(df) >= 50 else dinh_60

    # Vùng mua: từ (đáy 20 phiên hoặc giá - 1 ATR, lấy mức cao hơn) đến giá hiện tại
    mua_tu = max(day_20, gia - atr)
    mua_den = gia
    # Cắt lỗ: dưới đáy 20 phiên / dưới giá 1 ATR (mức thấp hơn), trừ thêm 1%
    cat_lo = min(day_20, gia - atr) * 0.99
    # Vùng bán: mục tiêu 1 = MA20, mục tiêu 2 = MA50 (nếu thấp hơn mục tiêu 1 thì lấy đỉnh 60 phiên)
    ban_1 = max(ma20, gia * 1.03)
    ban_2 = ma50 if ma50 > ban_1 else max(dinh_60, ban_1 * 1.03)

    rui_ro_pct = (gia - cat_lo) / gia * 100
    loi_1_pct = (ban_1 - gia) / gia * 100
    loi_2_pct = (ban_2 - gia) / gia * 100
    rr1 = loi_1_pct / rui_ro_pct if rui_ro_pct > 0 else 0
    rr2 = loi_2_pct / rui_ro_pct if rui_ro_pct > 0 else 0
    atr_pct = atr / gia * 100

    nhan_xet = []
    if atr_pct >= 4:
        nhan_xet.append(f"biến động mạnh (ATR {atr_pct:.1f}%/phiên)")
    elif atr_pct >= 2.5:
        nhan_xet.append(f"biến động trung bình (ATR {atr_pct:.1f}%/phiên)")
    else:
        nhan_xet.append(f"biến động thấp (ATR {atr_pct:.1f}%/phiên)")
    if vol_tb_10 < 100000:
        nhan_xet.append("thanh khoản mỏng, khó thoát lệnh nhanh")
    elif vol_tb_10 < 500000:
        nhan_xet.append("thanh khoản trung bình")
    else:
        nhan_xet.append("thanh khoản tốt")
    if rr1 < 1:
        nhan_xet.append("lợi nhuận mục tiêu 1 nhỏ hơn mức rủi ro (R:R < 1)")
    elif rr1 >= 2:
        nhan_xet.append("R:R mục tiêu 1 tốt (>= 2)")
    else:
        nhan_xet.append("R:R mục tiêu 1 ở mức vừa phải")
    if gia < ma50:
        nhan_xet.append("giá đang dưới MA50 (xu hướng trung hạn còn yếu)")

    return {
        'gia': gia, 'mua_tu': mua_tu, 'mua_den': mua_den, 'cat_lo': cat_lo,
        'ban_1': ban_1, 'ban_2': ban_2, 'rui_ro_pct': rui_ro_pct,
        'loi_1_pct': loi_1_pct, 'loi_2_pct': loi_2_pct, 'rr1': rr1, 'rr2': rr2,
        'nhan_xet': nhan_xet, 'atr_pct': atr_pct,
    }


def _tuyen_tinh(x, diem_0, diem_tot):
    """Quy đổi tuyến tính về 0..1: x = diem_0 -> 0, x = diem_tot -> 1."""
    if diem_tot == diem_0:
        return 0.0
    return max(0.0, min(1.0, (x - diem_0) / (diem_tot - diem_0)))


def kiem_tra_du_lieu(ch):
    """Trả về (danh sách cảnh báo nặng, danh sách cảnh báo nhẹ). Dùng để trừ điểm và gắn nhãn."""
    pt = ch['pt']
    nang, nhe = [], []
    if ch['pe'] is not None and ch['pe'] < 2:
        nang.append(f"P/E {ch['pe']:.1f} < 2 (có thể do lãi bất thường một lần hoặc dữ liệu sai)")
    if ch['roe'] > 60:
        nang.append(f"ROE {ch['roe']:.0f}% > 60% (bất thường, nên đối chiếu báo cáo tài chính)")
    if pt['gia'] < 5:
        nang.append(f"giá {pt['gia']*1000:,.0f}đ < 5.000đ (cổ phiếu penny, rủi ro cao)")
    if pt['atr_pct'] > 10:
        nang.append(f"biến động {pt['atr_pct']:.1f}%/phiên > 10%")
    elif pt['atr_pct'] > 8:
        nhe.append(f"biến động {pt['atr_pct']:.1f}%/phiên > 8%")
    if ch['roa'] > 0 and ch['roe'] / ch['roa'] > 20:
        nhe.append(f"ROE/ROA = {ch['roe']/ch['roa']:.0f} lần (đòn bẩy cao hoặc số liệu cần kiểm tra)")
    if ch['vol'] < 50000:
        nhe.append(f"khối lượng TB {ch['vol']:,.0f} < 50.000 (thanh khoản mỏng)")
    return nang, nhe


def cham_diem(ch):
    """Chấm điểm 0-100 rồi trừ điểm nếu dữ liệu bất thường.
    Cơ sở: quá bán 30, định giá 15, chất lượng 25, thanh khoản 10, R:R 15, biến động 5.
    Trừ điểm: mỗi cảnh báo nặng -15, mỗi cảnh báo nhẹ -7 (tối đa trừ 45)."""
    pt = ch['pt']
    qua_ban = 7.5 * (
        _tuyen_tinh(ch['rsi14'], 30, 15) + _tuyen_tinh(ch['rsi7'], 30, 10)
        + _tuyen_tinh(ch['mfi14'], 20, 10) + _tuyen_tinh(ch['mfi11'], 20, 10)
    )
    dinh_gia = 15 * _tuyen_tinh(ch['pe'], 10, 5) if ch['pe'] is not None else 0
    chat_luong = 12.5 * _tuyen_tinh(ch['roe'], 5, 20) + 12.5 * _tuyen_tinh(ch['roa'], 0, 5)
    thanh_khoan = 10 * _tuyen_tinh(ch['vol'], 20000, 500000)
    rr = 15 * _tuyen_tinh(pt['rr1'], 0, 2)
    bien_dong = 5 * _tuyen_tinh(pt['atr_pct'], 6, 2)
    co_so = qua_ban + dinh_gia + chat_luong + thanh_khoan + rr + bien_dong

    nang, nhe = kiem_tra_du_lieu(ch)
    tru = min(45, 15 * len(nang) + 7 * len(nhe))
    tong = max(0.0, co_so - tru)

    if tong >= 70:
        hang = "A"
    elif tong >= 55:
        hang = "B"
    elif tong >= 40:
        hang = "C"
    else:
        hang = "D"

    if nang:
        trang_thai = "🔴 Số liệu bất thường, cần kiểm tra kỹ trước khi tham khảo"
    elif nhe or tong < 55:
        trang_thai = "🟡 Cần cân nhắc thêm"
    else:
        trang_thai = "🟢 Đạt tiêu chí, số liệu nhất quán"

    return {
        'tong': tong, 'co_so': co_so, 'tru': tru, 'hang': hang,
        'qua_ban': qua_ban, 'dinh_gia': dinh_gia, 'chat_luong': chat_luong,
        'thanh_khoan': thanh_khoan, 'rr': rr, 'bien_dong': bien_dong,
        'canh_bao_nang': nang, 'canh_bao_nhe': nhe, 'trang_thai': trang_thai,
    }


def tao_tin_nhan(ma, ch):
    pt = ch['pt']
    pe_text = f"{ch['pe']:.1f}" if ch['pe'] is not None else "n/a"
    dong = [
        f"📌 {ma} — giá {pt['gia']:.2f}",
        f"🏅 Hạng #{ch['xep_hang']} | Điểm {ch['diem']['tong']:.0f}/100 (loại {ch['diem']['hang']})"
        + (f" — đã trừ {ch['diem']['tru']:.0f} điểm do dữ liệu bất thường" if ch['diem']['tru'] else ""),
        f"🚦 {ch['diem']['trang_thai']}",
        f"   Quá bán {ch['diem']['qua_ban']:.0f}/30 · Định giá {ch['diem']['dinh_gia']:.0f}/15 · Chất lượng {ch['diem']['chat_luong']:.0f}/25",
        f"   Thanh khoản {ch['diem']['thanh_khoan']:.0f}/10 · R:R {ch['diem']['rr']:.0f}/15 · Biến động {ch['diem']['bien_dong']:.0f}/5",
        "",
        "📊 CHỈ SỐ",
        f"• RSI(14): {ch['rsi14']:.1f}   • RSI(7): {ch['rsi7']:.1f}",
        f"• MFI(14): {ch['mfi14']:.1f}   • MFI(11): {ch['mfi11']:.1f}",
        f"• ROA: {ch['roa']:.2f}%   • ROE: {ch['roe']:.2f}%",
        f"• P/E: {pe_text}   • EPS 4Q: {ch['eps']:,.0f}",
        f"• KL TB 10 phiên: {ch['vol']:,.0f}",
        "",
        "🎯 KỊCH BẢN (tính theo công thức, đơn vị nghìn đồng)",
        f"• Vùng mua: {pt['mua_tu']:.2f} – {pt['mua_den']:.2f}",
        f"• Vùng bán 1: {pt['ban_1']:.2f} (+{pt['loi_1_pct']:.1f}%)",
        f"• Vùng bán 2: {pt['ban_2']:.2f} (+{pt['loi_2_pct']:.1f}%)",
        f"• Cắt lỗ: {pt['cat_lo']:.2f} (-{pt['rui_ro_pct']:.1f}%)",
        f"• Rủi ro/Lợi nhuận: R:R1 = {pt['rr1']:.1f} | R:R2 = {pt['rr2']:.1f}",
        "",
        "⚠️ RỦI RO: " + "; ".join(pt['nhan_xet']) + ".",
    ]
    canh_bao = ch['diem']['canh_bao_nang'] + ch['diem']['canh_bao_nhe']
    if canh_bao:
        dong.append("")
        dong.append("🔎 CẦN KIỂM TRA LẠI:")
        dong.extend(f"• {c}" for c in canh_bao)
    return "\n".join(dong)


ds_ma_goc = kbs_client.danh_sach_co_phieu()

ma_loi_cu = doc_ma_loi()
ds_ma = [m for m in ds_ma_goc if m not in ma_loi_cu]

print(f"Ngày quét: {ngay_hom_nay}")
print(f"Tổng số mã: {len(ds_ma_goc)}, đã loại {len(ma_loi_cu)} mã lỗi cũ, còn quét: {len(ds_ma)}")

ma_vao_vung_mua = []
chi_tiet = {}

for i, ma in enumerate(ds_ma):
    print(f"[{i+1}/{len(ds_ma)}] Đang quét: {ma}")
    try:
        df = kbs_client.lich_su_gia(ma, ngay_bat_dau, ngay_hom_nay)
        if df is None or len(df) < 15:
            ghi_ma_loi(ma)
            continue

        df = df[df['volume'] > 0]
        if len(df) < 15:
            ghi_ma_loi(ma)
            continue

        df['RSI14'] = ta.momentum.RSIIndicator(close=df['close'], window=14).rsi()
        df['MFI14'] = ta.volume.MFIIndicator(high=df['high'], low=df['low'], close=df['close'], volume=df['volume'], window=14).money_flow_index()
        # RSI7 và MFI11 chỉ để hiển thị, KHÔNG dùng làm điều kiện lọc
        df['RSI7'] = ta.momentum.RSIIndicator(close=df['close'], window=7).rsi()
        df['MFI11'] = ta.volume.MFIIndicator(high=df['high'], low=df['low'], close=df['close'], volume=df['volume'], window=11).money_flow_index()

        vol_tb_10 = df['volume'].tail(10).mean()
        rsi_hien_tai = df['RSI14'].iloc[-1]
        mfi_hien_tai = df['MFI14'].iloc[-1]
        gia_dong_cua_moi_nhat = df['close'].iloc[-1]

        chi_so = kbs_client.chi_so_tai_chinh(ma, limit=4)
        if chi_so.empty:
            ghi_ma_loi(ma)
            continue
        cot_gan_nhat = [c for c in chi_so.columns if c not in ['item', 'item_en', 'item_id']][0]

        eps_ttm = lay_gia_tri(chi_so, ['trailing_eps'], cot_gan_nhat)
        roa = lay_gia_tri(chi_so, ['roa_trailling', 'roa'], cot_gan_nhat)
        roe = lay_gia_tri(chi_so, ['roe_trailling', 'roe'], cot_gan_nhat)

        pe = (gia_dong_cua_moi_nhat * 1000) / eps_ttm if eps_ttm else None

        dat_kl = vol_tb_10 >= 20000
        dat_rsi = rsi_hien_tai < 30
        dat_mfi = mfi_hien_tai < 20
        dat_pe = pe is not None and pe < 10
        dat_roa = roa > 0
        dat_roe = roe > 5

        if dat_kl and dat_rsi and dat_mfi and dat_pe and dat_roa and dat_roe:
            ma_vao_vung_mua.append(ma)
            chi_tiet[ma] = {
                'rsi14': rsi_hien_tai, 'rsi7': df['RSI7'].iloc[-1],
                'mfi14': mfi_hien_tai, 'mfi11': df['MFI11'].iloc[-1],
                'roa': roa, 'roe': roe, 'pe': pe, 'eps': eps_ttm, 'vol': vol_tb_10,
                'pt': phan_tich_ma(df, roa, roe, pe, eps_ttm, vol_tb_10),
            }
            print(f"  -> ✅ {ma} VÀO VÙNG MUA!")

    except Exception as e:
        print(f"  -> Lỗi với {ma}: {e}")
        ghi_ma_loi(ma)

    time.sleep(2.5)

if ma_vao_vung_mua:
    for ma in ma_vao_vung_mua:
        chi_tiet[ma]['diem'] = cham_diem(chi_tiet[ma])
    xep = sorted(ma_vao_vung_mua, key=lambda m: chi_tiet[m]['diem']['tong'], reverse=True)
    for hang, ma in enumerate(xep, start=1):
        chi_tiet[ma]['xep_hang'] = hang

    bang = [f"🔔 CẢNH BÁO VÙNG MUA ({ngay_hom_nay})", f"Có {len(xep)} mã đạt đủ điều kiện, xếp hạng theo điểm:", ""]
    for hang, ma in enumerate(xep, start=1):
        d = chi_tiet[ma]['diem']
        bang.append(f"#{hang} {ma} — {d['tong']:.0f}/100 ({d['hang']}) {d['trang_thai'][:2]}")
    gui_telegram("\n".join(bang))

    for ma in xep:
        gui_telegram(tao_tin_nhan(ma, chi_tiet[ma]))
        time.sleep(1)
    gui_telegram("ℹ️ CÁCH ĐỌC: điểm 0-100 = quá bán 30 + định giá 15 + chất lượng 25 + thanh khoản 10 + R:R 15 + biến động 5, "
                 "rồi trừ điểm nếu dữ liệu bất thường (nặng -15, nhẹ -7). A>=70, B>=55, C>=40, D<40.\n"
                 "🟢 đạt tiêu chí và số liệu nhất quán · 🟡 cần cân nhắc thêm · 🔴 số liệu bất thường.\n"
                 "Đây là số liệu và kịch bản tính tự động, chưa kiểm chứng bằng dữ liệu lịch sử, không phải lời khuyên mua/bán. "
                 "Người dùng tự nghiên cứu và tự chịu trách nhiệm với quyết định của mình.")
else:
    gui_telegram(f"Quét xong ({ngay_hom_nay}): không có mã nào vào vùng mua lúc này.")

print("\nHoàn tất!")