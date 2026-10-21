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


def _so(x):
    """Ép về float, trả None nếu không phải số hợp lệ."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def lay_tuy_chon(df_ratio, danh_sach_id, cot):
    """Giống lay_gia_tri nhưng trả None nếu KBS không có chỉ số (không làm bot dừng)."""
    for id_can_tim in danh_sach_id:
        hang = df_ratio[df_ratio['item_id'] == id_can_tim]
        if not hang.empty:
            v = _so(hang[cot].values[0])
            if v is not None:
                return v
    return None


def lay_chuoi(df_ratio, danh_sach_id):
    """Chuỗi giá trị các quý (mới nhất trước) của một chỉ số, bỏ qua quý thiếu."""
    cot_quy = [c for c in df_ratio.columns if c not in ['item', 'item_en', 'item_id']]
    for id_can_tim in danh_sach_id:
        hang = df_ratio[df_ratio['item_id'] == id_can_tim]
        if not hang.empty:
            return [_so(hang[c].values[0]) for c in cot_quy]
    return []


def tinh_chi_so_tai_chinh(chi_so, cot, gia_dong_cua, eps_ttm, roa, roe):
    """Các chỉ số sức khỏe doanh nghiệp. Chỉ số nào KBS không có thì là None."""
    tc = {}
    # Đòn bẩy tài chính = Tổng tài sản / Vốn chủ = ROE / ROA  (cùng đơn vị nên không lệch đơn vị)
    tc['don_bay'] = roe / roa if roa and roa > 0 and roe is not None else None
    # Nợ phải trả / Vốn chủ: ưu tiên số KBS (đơn vị %), thiếu thì ước tính từ ROE/ROA
    lte = lay_tuy_chon(chi_so, ['liabilities_to_equity'], cot)
    tc['de'] = lte / 100 if lte is not None else (tc['don_bay'] - 1 if tc['don_bay'] else None)
    tc['da'] = 1 - roa / roe if roe and roe > 0 and roa is not None else None  # Nợ / Tổng tài sản
    # Tổ chức tài chính (ngân hàng, chứng khoán...) có đòn bẩy rất cao: không đánh giá bằng D/E và thanh toán hiện hành
    tc['la_tctc'] = bool((tc['don_bay'] and tc['don_bay'] > 8) or (tc['de'] is not None and tc['de'] > 8))

    # Giá trị sổ sách / cổ phiếu (VND): ưu tiên số KBS, thiếu thì ước tính = EPS / ROE
    bvps = lay_tuy_chon(chi_so, ['book_value_per_share_bvps', 'book_value_per_share', 'bvps'], cot)
    if not bvps or bvps <= 0:
        bvps = (eps_ttm / (roe / 100)) if eps_ttm and roe and roe > 0 else None
    tc['bvps'] = bvps
    # ROE kiểm chéo = EPS 4 quý / giá trị sổ sách KBS (chỉ khi có số BVPS thật của KBS, tránh tính vòng tròn)
    bvps_kbs = lay_tuy_chon(chi_so, ['book_value_per_share_bvps', 'book_value_per_share', 'bvps'], cot)
    tc['roe_chk'] = (eps_ttm / bvps_kbs * 100) if bvps_kbs and bvps_kbs > 0 and eps_ttm else None
    tc['pb'] = (gia_dong_cua * 1000) / bvps if bvps and bvps > 0 else None

    cr = lay_tuy_chon(chi_so, ['current_ratio', 'short_term_ratio'], cot)
    if cr is not None and cr > 20:   # nếu KBS trả theo %
        cr = cr / 100
    tc['cr'] = cr
    tc['gm'] = lay_tuy_chon(chi_so, ['gross_profit_margin', 'gross_margin'], cot)
    tc['nm'] = lay_tuy_chon(chi_so, ['net_profit_margin', 'net_margin'], cot)

    # Tăng trưởng EPS 4 quý (TTM) so với cùng kỳ năm trước và xu hướng 3 quý gần nhất
    chuoi_eps = lay_chuoi(chi_so, ['trailing_eps'])
    tc['eps_yoy'] = None
    if len(chuoi_eps) >= 5 and chuoi_eps[0] is not None and chuoi_eps[4] and chuoi_eps[4] > 0:
        tc['eps_yoy'] = (chuoi_eps[0] - chuoi_eps[4]) / chuoi_eps[4] * 100
    tc['eps_xu_huong'] = None
    cuoi = [x for x in chuoi_eps[:4] if x is not None]
    if len(cuoi) >= 4:
        # cuoi[0] là quý mới nhất. Đếm số lần EPS TTM tăng liên tiếp qua 3 quý gần nhất
        tc['eps_xu_huong'] = sum(1 for k in range(3) if cuoi[k] > cuoi[k + 1])
    return tc


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
    """Trả về (cảnh báo nặng, cảnh báo nhẹ). Dùng để trừ điểm và gắn nhãn."""
    pt, tc = ch['pt'], ch['tc']
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
    if tc['eps_yoy'] is not None and tc['eps_yoy'] < -30:
        nang.append(f"EPS 4 quý giảm {abs(tc['eps_yoy']):.0f}% so với cùng kỳ năm trước")
    if tc['eps_yoy'] is not None and tc['eps_yoy'] > 300:
        nhe.append(f"EPS 4 quý tăng {tc['eps_yoy']:.0f}% so với cùng kỳ (có thể do lãi một lần, không bền vững)")
    roe_chk = tc.get('roe_chk')
    if roe_chk is not None and roe_chk > 0 and ch['roe'] > 0:
        ti_le = ch['roe'] / roe_chk
        if ti_le > 1.6 or ti_le < 0.6:
            nhe.append(f"ROE KBS {ch['roe']:.1f}% lệch nhiều so với EPS/giá trị sổ sách ({roe_chk:.1f}%), nên đối chiếu báo cáo tài chính")
    if ch['roa'] > 0 and ch['roe'] / ch['roa'] > 20:
        nhe.append(f"ROE/ROA = {ch['roe']/ch['roa']:.0f} lần (đòn bẩy cao hoặc số liệu cần kiểm tra)")
    if ch['vol'] < 50000:
        nhe.append(f"khối lượng TB {ch['vol']:,.0f} < 50.000 (thanh khoản mỏng)")
    if tc['pb'] is not None and tc['pb'] < 0.3:
        nhe.append(f"P/B {tc['pb']:.2f} < 0.3 (giá thấp xa giá trị sổ sách, nên kiểm tra tài sản/nợ)")
    if not tc['la_tctc']:
        if tc['de'] is not None and tc['de'] > 3:
            nhe.append(f"Nợ/Vốn chủ ~ {tc['de']:.1f} lần > 3 (đòn bẩy cao)")
        if tc['cr'] is not None and tc['cr'] < 1:
            nhe.append(f"thanh toán hiện hành {tc['cr']:.2f} < 1 (tài sản ngắn hạn không đủ trả nợ ngắn hạn)")
    return nang, nhe


def cham_diem(ch):
    """Chấm điểm 0-100, 6 nhóm. Chỉ số nào KBS không có thì bỏ ra khỏi mẫu số (không phạt oan).
    Quá bán 20 | Định giá 20 | Sinh lời 20 | Tăng trưởng 15 | An toàn tài chính 15 | Thanh khoản & R:R 10.
    Sau đó trừ điểm cảnh báo (nặng -15, nhẹ -7, tối đa -45)."""
    pt, tc = ch['pt'], ch['tc']

    def nhom(*thanh_phan):
        """thanh_phan: (điểm_tối_đa, giá_trị_0_đến_1 hoặc None). Trả (điểm đạt, điểm tối đa khả dụng)."""
        dat = sum(m * v for m, v in thanh_phan if v is not None)
        kd = sum(m for m, v in thanh_phan if v is not None)
        return dat, kd

    # Quá bán (kỹ thuật)
    qua_ban = nhom(
        (5, _tuyen_tinh(ch['rsi14'], 30, 15)), (5, _tuyen_tinh(ch['rsi7'], 30, 10)),
        (5, _tuyen_tinh(ch['mfi14'], 20, 10)), (5, _tuyen_tinh(ch['mfi11'], 20, 10)),
    )
    # Định giá: P/E và P/B (P/B < 0.3 chỉ được nửa điểm vì thường là dấu hiệu bất thường)
    v_pb = None
    if tc['pb'] is not None:
        v_pb = _tuyen_tinh(tc['pb'], 3, 1)
        if tc['pb'] < 0.3:
            v_pb *= 0.5
    dinh_gia = nhom(
        (10, _tuyen_tinh(ch['pe'], 10, 5) if ch['pe'] is not None else None),
        (10, v_pb),
    )
    # Sinh lời & chất lượng
    sinh_loi = nhom(
        (8, _tuyen_tinh(ch['roe'], 5, 20)), (6, _tuyen_tinh(ch['roa'], 0, 5)),
        (6, _tuyen_tinh(tc['nm'], 0, 10) if tc['nm'] is not None else None),
    )
    # Tăng trưởng
    tang_truong = nhom(
        (10, _tuyen_tinh(tc['eps_yoy'], -20, 20) if tc['eps_yoy'] is not None else None),
        (5, tc['eps_xu_huong'] / 3 if tc['eps_xu_huong'] is not None else None),
    )
    # An toàn tài chính (tổ chức tài chính: không chấm D/E và thanh toán hiện hành)
    if tc['la_tctc']:
        an_toan = nhom((8, None), (7, None))
    else:
        an_toan = nhom(
            (8, _tuyen_tinh(tc['de'], 2.5, 0.5) if tc['de'] is not None else None),
            (7, _tuyen_tinh(tc['cr'], 0.8, 1.5) if tc['cr'] is not None else None),
        )
    # Thanh khoản và R:R (R:R chỉ tham khảo nên trọng số nhỏ)
    tk_rr = nhom(
        (5, _tuyen_tinh(ch['vol'], 20000, 500000)), (5, _tuyen_tinh(pt['rr1'], 0, 2)),
    )

    cac_nhom = {
        'qua_ban': (qua_ban, 20), 'dinh_gia': (dinh_gia, 20), 'sinh_loi': (sinh_loi, 20),
        'tang_truong': (tang_truong, 15), 'an_toan': (an_toan, 15), 'tk_rr': (tk_rr, 10),
    }
    tong_dat = sum(v[0][0] for v in cac_nhom.values())
    tong_kd = sum(v[0][1] for v in cac_nhom.values())
    co_so = tong_dat / tong_kd * 100 if tong_kd else 0.0
    do_day_du = tong_kd / 100  # tỉ lệ chỉ số có dữ liệu

    nang, nhe = kiem_tra_du_lieu(ch)
    if do_day_du < 0.7:
        nhe.append(f"thiếu nhiều chỉ số tài chính (chỉ có {do_day_du*100:.0f}% dữ liệu), điểm kém tin cậy")
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

    chi_tiet_nhom = {k: (v[0][0] / v[0][1] * v[1] if v[0][1] else None, v[1]) for k, v in cac_nhom.items()}
    return {
        'tong': tong, 'co_so': co_so, 'tru': tru, 'hang': hang, 'do_day_du': do_day_du,
        'nhom': chi_tiet_nhom,
        'canh_bao_nang': nang, 'canh_bao_nhe': nhe, 'trang_thai': trang_thai,
    }


def _f(x, dinh_dang, don_vi=""):
    return "n/a" if x is None else f"{x:{dinh_dang}}{don_vi}"


def tao_tin_nhan(ma, ch):
    pt, tc, d = ch['pt'], ch['tc'], ch['diem']
    pe_text = _f(ch['pe'], ".1f")
    n = d['nhom']

    def diem(k):
        v = n[k][0]
        return "bỏ qua" if v is None else f"{v:.0f}/{n[k][1]}"

    dong = [
        f"📌 {ma} — giá {pt['gia']:.2f}",
        f"🏅 Hạng #{ch['xep_hang']} | Điểm {d['tong']:.0f}/100 (loại {d['hang']})"
        + (f" — đã trừ {d['tru']:.0f} điểm do cảnh báo" if d['tru'] else ""),
        f"🚦 {d['trang_thai']}",
        f"   Quá bán {diem('qua_ban')} · Định giá {diem('dinh_gia')} · Sinh lời {diem('sinh_loi')}",
        f"   Tăng trưởng {diem('tang_truong')} · An toàn TC {diem('an_toan')} · Thanh khoản & R:R {diem('tk_rr')}",
        f"   Dữ liệu tài chính đầy đủ: {d['do_day_du']*100:.0f}%",
        "",
        "📊 KỸ THUẬT",
        f"• RSI(14): {ch['rsi14']:.1f}   • RSI(7): {ch['rsi7']:.1f}",
        f"• MFI(14): {ch['mfi14']:.1f}   • MFI(11): {ch['mfi11']:.1f}",
        f"• KL TB 10 phiên: {ch['vol']:,.0f}",
        "",
        "🏢 SỨC KHỎE DOANH NGHIỆP",
        f"• ROA: {ch['roa']:.2f}%   • ROE: {ch['roe']:.2f}%",
        f"• P/E: {pe_text}   • P/B: {_f(tc['pb'], '.2f')}   • EPS 4Q: {ch['eps']:,.0f}",
        f"• Biên LN gộp: {_f(tc['gm'], '.1f', '%')}   • Biên LN ròng: {_f(tc['nm'], '.1f', '%')}",
        f"• Tăng trưởng EPS cùng kỳ: {_f(tc['eps_yoy'], '+.0f', '%')}   • EPS TTM tăng liên tiếp: {_f(tc['eps_xu_huong'], '.0f', '/3 quý')}",
        (f"• Nợ/Vốn chủ: không đánh giá (đòn bẩy {tc['don_bay']:.0f} lần, giống tổ chức tài chính)"
         if tc['la_tctc'] else
         f"• Nợ/Vốn chủ ~ {_f(tc['de'], '.2f')} lần   • Thanh toán hiện hành: {_f(tc['cr'], '.2f')}"),
        "",
        "🎯 KỊCH BẢN KỸ THUẬT (tính theo công thức, đơn vị nghìn đồng)",
        f"• Vùng mua: {pt['mua_tu']:.2f} – {pt['mua_den']:.2f}",
        f"• Vùng bán 1: {pt['ban_1']:.2f} (+{pt['loi_1_pct']:.1f}%)",
        f"• Vùng bán 2: {pt['ban_2']:.2f} (+{pt['loi_2_pct']:.1f}%)",
        f"• Cắt lỗ: {pt['cat_lo']:.2f} (-{pt['rui_ro_pct']:.1f}%)",
        f"• Rủi ro/Lợi nhuận: R:R1 = {pt['rr1']:.1f} | R:R2 = {pt['rr2']:.1f}",
        "",
        "⚠️ RỦI RO: " + "; ".join(pt['nhan_xet']) + ".",
    ]
    canh_bao = d['canh_bao_nang'] + d['canh_bao_nhe']
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

        chi_so = kbs_client.chi_so_tai_chinh(ma, limit=5)
        if chi_so.empty:
            ghi_ma_loi(ma)
            continue
        cot_gan_nhat = [c for c in chi_so.columns if c not in ['item', 'item_en', 'item_id']][0]

        if i == 0:
            print("  Các chỉ số KBS có:", ", ".join(chi_so['item_id'].astype(str)))
        eps_ttm = lay_gia_tri(chi_so, ['trailing_eps'], cot_gan_nhat)
        roa = lay_gia_tri(chi_so, ['roa_trailling', 'roa'], cot_gan_nhat)
        roe = lay_gia_tri(chi_so, ['roe_trailling', 'roe'], cot_gan_nhat)

        pe = (gia_dong_cua_moi_nhat * 1000) / eps_ttm if eps_ttm else None

        dat_kl = vol_tb_10 >= 20000
        dat_rsi = rsi_hien_tai < 30
        dat_mfi = mfi_hien_tai < 20
        dat_pe = pe is not None and 0 < pe < 10  # EPS âm thì P/E âm, không tính là rẻ
        dat_roa = roa > 0
        dat_roe = roe > 5

        if dat_kl and dat_rsi and dat_mfi and dat_pe and dat_roa and dat_roe:
            ma_vao_vung_mua.append(ma)
            chi_tiet[ma] = {
                'rsi14': rsi_hien_tai, 'rsi7': df['RSI7'].iloc[-1],
                'mfi14': mfi_hien_tai, 'mfi11': df['MFI11'].iloc[-1],
                'roa': roa, 'roe': roe, 'pe': pe, 'eps': eps_ttm, 'vol': vol_tb_10,
                'pt': phan_tich_ma(df, roa, roe, pe, eps_ttm, vol_tb_10),
                'tc': tinh_chi_so_tai_chinh(chi_so, cot_gan_nhat, gia_dong_cua_moi_nhat, eps_ttm, roa, roe),
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
    gui_telegram("ℹ️ CÁCH ĐỌC: điểm 0-100 = quá bán 20 + định giá (P/E, P/B) 20 + sinh lời (ROE, ROA, biên LN) 20 "
                 "+ tăng trưởng EPS 15 + an toàn tài chính (nợ/vốn chủ, thanh toán hiện hành) 15 + thanh khoản & R:R 10. "
                 "Chỉ số nào không có dữ liệu thì bỏ khỏi phép tính. Sau đó trừ điểm cảnh báo (nặng -15, nhẹ -7). "
                 "A>=70, B>=55, C>=40, D<40.\n"
                 "🟢 đạt tiêu chí và số liệu nhất quán · 🟡 cần cân nhắc thêm · 🔴 số liệu bất thường.\n"
                 "Điểm chưa xét tin tức, ngành, dòng tiền và chất lượng lợi nhuận, chưa kiểm chứng bằng dữ liệu lịch sử, "
                 "không phải lời khuyên mua/bán. Người dùng tự nghiên cứu và tự chịu trách nhiệm với quyết định của mình.")
else:
    gui_telegram(f"Quét xong ({ngay_hom_nay}): không có mã nào vào vùng mua lúc này.")

print("\nHoàn tất!")