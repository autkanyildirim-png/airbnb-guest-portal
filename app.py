import streamlit as st
from datetime import datetime, timedelta, date
import uuid
import urllib.parse
import re
import json
import requests
from icalendar import Calendar
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, joinedload

Base = declarative_base()

class ServiceListing(Base):
    __tablename__ = "portal_services_v20"
    id = Column(Integer, primary_key=True, index=True)
    service_type = Column(String(20), nullable=False)
    title = Column(String(150), nullable=False)
    location = Column(String(255), nullable=False)
    organizer_phone = Column(String(30), nullable=False)
    ical_url = Column(String(500), nullable=True)
    
    wifi_ssid = Column(String(50), nullable=True)
    wifi_password = Column(String(50), nullable=True)
    lockbox_code = Column(String(50), nullable=True)
    checkin_time = Column(String(10), default="15:00")
    checkout_time = Column(String(10), default="11:00")
    
    boat_name = Column(String(100), nullable=True)
    capacity = Column(String(20), nullable=True)
    price_details = Column(String(100), nullable=True)
    tour_route = Column(Text, nullable=True)
    active_days = Column(Text, nullable=True)
    defined_slots = Column(Text, nullable=True)
    
    details_and_rules = Column(Text)
    reservations = relationship("Reservation", back_populates="service", cascade="all, delete-orphan")
    alerts = relationship("AlertLog", back_populates="service", cascade="all, delete-orphan")

class Reservation(Base):
    __tablename__ = "portal_reservations_v20"
    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(String(50), unique=True, index=True)
    feedback_token = Column(String(50), unique=True, index=True)
    
    service_id = Column(Integer, ForeignKey("portal_services_v20.id"), nullable=False)
    guest_name = Column(String(100), nullable=False)
    guest_phone = Column(String(30), nullable=True)
    phone_verified = Column(String(10), default="Hayır")
    source = Column(String(30), default="Manuel")
    selected_slot = Column(String(150), nullable=True)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    
    rating = Column(Integer, nullable=True)
    review_note = Column(Text, nullable=True)
    review_submitted_at = Column(DateTime, nullable=True)
    
    service = relationship("ServiceListing", back_populates="reservations")
    alerts = relationship("AlertLog", back_populates="reservation", cascade="all, delete-orphan")

class AlertLog(Base):
    __tablename__ = "portal_alerts_v20"
    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("portal_services_v20.id"), nullable=False)
    reservation_id = Column(Integer, ForeignKey("portal_reservations_v20.id"), nullable=False)
    alert_type = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    
    service = relationship("ServiceListing", back_populates="alerts")
    reservation = relationship("Reservation", back_populates="alerts")

engine = create_engine("sqlite:///./guest_portal_v20.db", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def sanitize_phone_number(phone: str) -> str:
    if not phone:
        return ""
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 11 and digits.startswith('0'):
        digits = '9' + digits
    elif len(digits) == 10 and digits.startswith('5'):
        digits = '90' + digits
    return digits

def build_wa_url(phone: str, message: str) -> str:
    clean_p = sanitize_phone_number(phone)
    encoded = urllib.parse.quote(message)
    return f"https://api.whatsapp.com/send?phone={clean_p}&text={encoded}"

def sync_service_calendar(srv_id: int):
    db = SessionLocal()
    added_count = 0
    try:
        srv = db.query(ServiceListing).filter(ServiceListing.id == srv_id).first()
        if not srv or not srv.ical_url or not srv.ical_url.startswith("http"):
            return False, "Geçerli bir Airbnb iCal URL'si tanımlı değil."
        
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(srv.ical_url, headers=headers, timeout=15)
        if response.status_code != 200:
            return False, f"Takvim indirilemedi (Hata Kodu: {response.status_code})."
        
        cal = Calendar.from_ical(response.content)
        for event in cal.walk('vevent'):
            summary = str(event.get('summary', 'Airbnb Misafiri'))
            dtstart_raw = event.get('dtstart').dt
            dtend_raw = event.get('dtend').dt
            
            if isinstance(dtstart_raw, date) and not isinstance(dtstart_raw, datetime):
                start_dt = datetime.combine(dtstart_raw, datetime.min.time()).replace(hour=10 if srv.service_type == "yat" else 15)
            else:
                start_dt = dtstart_raw
                
            if isinstance(dtend_raw, date) and not isinstance(dtend_raw, datetime):
                end_dt = datetime.combine(dtend_raw, datetime.min.time()).replace(hour=18 if srv.service_type == "yat" else 11)
            else:
                end_dt = dtend_raw

            existing = db.query(Reservation).filter(
                Reservation.service_id == srv.id,
                Reservation.start_time == start_dt
            ).first()
            
            if not existing:
                chat_tok = str(uuid.uuid4())[:8]
                rev_tok = str(uuid.uuid4())[:8]
                
                slot_val = None
                if srv.service_type == "yat" and srv.defined_slots:
                    try:
                        slots_list = json.loads(srv.defined_slots)
                        slot_val = slots_list[0] if slots_list else "Günlük Tur"
                    except Exception:
                        slot_val = "Günlük Tur"

                new_r = Reservation(
                    access_token=chat_tok,
                    feedback_token=rev_tok,
                    service_id=srv.id,
                    guest_name=summary if summary != "Reserved" else ("Airbnb Yat Misafiri" if srv.service_type=="yat" else "Airbnb Ev Misafiri"),
                    guest_phone="",
                    phone_verified="Hayır",
                    source="Airbnb",
                    selected_slot=slot_val,
                    start_time=start_dt,
                    end_time=end_dt
                )
                db.add(new_r)
                added_count += 1
                
        db.commit()
        return True, f"Senkronizasyon tamamlandı! {added_count} yeni Airbnb rezervasyonu içe aktarıldı."
    except Exception as e:
        return False, f"Hata oluştu: {str(e)}"
    finally:
        db.close()

def portal_ai_engine(guest_msg, srv, res):
    msg_lower = guest_msg.lower()
    is_emergency = any(w in msg_lower for w in ["acil", "ambulans", "doktor", "kaza", "hastane", "fenalaştı", "yangın", "polis", "yardım", "hırsız"])
    is_delay = any(w in msg_lower for w in ["geç kal", "gecik", "trafik", "yetişeme", "15 dk", "yarım saat", "yoldayız", "rötar", "gecike"])
    is_cancel = any(w in msg_lower for w in ["gelemiyorum", "iptal", "işim çıktı", "vazgeçtim"])
    
    if is_emergency:
        with SessionLocal() as db:
            db.add(AlertLog(service_id=srv.id, reservation_id=res.id, alert_type="🚨 ACİL DURUM", message=guest_msg))
            db.commit()
        return f"🚨 Durumunuz sisteme işlendi {res.guest_name}! Çok geçmiş olsun. İşletme yetkilimize bildirim iletilmiştir. Yetkili hattı: **{srv.organizer_phone}**", True, "🚨 ACİL DURUM"
        
    elif is_delay:
        with SessionLocal() as db:
            db.add(AlertLog(service_id=srv.id, reservation_id=res.id, alert_type="⏱️ GECİKME", message=guest_msg))
            db.commit()
        return f"⏱️ Bilgilendirme için teşekkürler {res.guest_name}. Gecikme bildiriminiz yönetici paneline kaydedildi.", True, "⏱️ GECİKME BİLDİRİMİ"
        
    elif is_cancel:
        with SessionLocal() as db:
            db.add(AlertLog(service_id=srv.id, reservation_id=res.id, alert_type="🚫 İPTAL", message=guest_msg))
            db.commit()
        return f"🚫 İptal talebiniz yönetici paneline işlendi {res.guest_name}.", True, "🚫 İPTAL BİLDİRİMİ"
        
    if srv.service_type == "ev":
        if any(w in msg_lower for w in ["wifi", "şifre", "internet", "kablosuz"]):
            return f"📶 **Wi-Fi Ağ Adı:** {srv.wifi_ssid}\n🔑 **Şifre:** `{srv.wifi_password}`", False, None
        elif any(w in msg_lower for w in ["kapı", "kod", "kasa", "kilit", "anahtar", "nasıl gir"]):
            return f"🔑 **Kapı / Kasa Kodunuz:** `{srv.lockbox_code}`", False, None
        elif any(w in msg_lower for w in ["çıkış", "giriş", "saat", "kaçta"]):
            return f"⏰ Giriş saati: **{srv.checkin_time}**, Çıkış saati: **{srv.checkout_time}**'dir.", False, None
        elif any(w in msg_lower for w in ["nerede", "adres", "konum", "nasıl giderim"]):
            return f"📍 **Açık Adres:** {srv.location}", False, None
        elif any(w in msg_lower for w in ["sahip", "yetkili", "telefon", "numara", "iletişim"]):
            return f"📞 **Ev Sahibi WhatsApp:** `{srv.organizer_phone}`", False, None
        else:
            return f"📖 **Ev Kuralları & Rehberi:**\n{srv.details_and_rules}", False, None
    else:
        if any(w in msg_lower for w in ["rota", "koy", "nereye", "yüzme", "güzergah"]):
            return f"🌊 **Tur Rotamız & Koylar:**\n{srv.tour_route}", False, None
        elif any(w in msg_lower for w in ["nereden", "marina", "konum", "iskele", "buluşma"]):
            return f"📍 **Buluşma Noktası:** {srv.location}\n⚓ **Tekne:** {srv.boat_name}\n📞 **Kaptan Tel:** {srv.organizer_phone}", False, None
        elif any(w in msg_lower for w in ["yemek", "içecek", "menü", "meyve", "dahil", "ikram"]):
            return f"🍇 **İkramlar & Dahil Olanlar:**\n{srv.details_and_rules}", False, None
        elif any(w in msg_lower for w in ["seans", "saat", "kaçta"]):
            return f"⏰ **Rezervasyon Seansınız:** {res.selected_slot}", False, None
        elif any(w in msg_lower for w in ["kaptan", "yetkili", "telefon", "iletişim"]):
            return f"📞 **Kaptan WhatsApp:** `{srv.organizer_phone}`", False, None
        else:
            return f"⛵ **Yat Turu Bilgileri:**\n{srv.details_and_rules}", False, None

st.set_page_config(page_title="AI Rezervasyon & Misafir Portalı", page_icon="🌟", layout="wide")

q_params = st.query_params
token_chat = q_params.get("chat", None)
token_review = q_params.get("review", None)

if token_chat:
    active_route = "chat"
elif token_review:
    active_route = "review"
else:
    active_route = "admin"

if active_route == "chat":
    with SessionLocal() as db:
        res = db.query(Reservation).options(joinedload(Reservation.service)).filter(Reservation.access_token == token_chat).first()
        if not res:
            st.error("❌ Geçersiz bağlantı.")
        else:
            srv = res.service
            is_phone_ready = bool(res.guest_phone and len(res.guest_phone.strip()) >= 10 and res.phone_verified == "Evet")
            
            if not is_phone_ready:
                st.title("🔒 Rezervasyon Bilgilerine Erişim Kapısı")
                st.markdown(f"### Hoş Geldiniz, **{res.guest_name}** 👋")
                st.info(f"**{srv.title}** için kapı şifresi/konum rehberi ve 7/24 Yapay Zeka Concierge Asistanınıza erişmek için lütfen WhatsApp numaranızı doğrulayın.")
                
                with st.form("phone_gate_form"):
                    input_ph = st.text_input("📲 WhatsApp Telefon Numaranız (Örn: 05321112233)", placeholder="05xxxxxxxxx")
                    submit_phone = st.form_submit_button("Rehbere ve Asistana Giriş Yap 🚀", type="primary", use_container_width=True)
                    
                    if submit_phone:
                        clean_num = sanitize_phone_number(input_ph)
                        if len(clean_num) < 10:
                            st.error("Lütfen geçerli bir telefon numarası giriniz (en az 10 hane).")
                        else:
                            res.guest_phone = input_ph
                            res.phone_verified = "Evet"
                            db.commit()
                            st.success("✅ Numaranız doğrulandı! Asistan açılıyor...")
                            st.rerun()
            else:
                st.title("🤖 7/24 Dijital Concierge Asistanınız")
                st.markdown(f"### Merhaba **{res.guest_name}**, hoş geldiniz! 👋")
                st.caption(f"📍 **{srv.title}** | 📅 {res.start_time.strftime('%d.%m.%Y')} — {res.end_time.strftime('%d.%m.%Y')}")
                
                with st.expander("📌 Rezervasyon Özeti & Temel Bilgiler", expanded=True):
                    if srv.service_type == "ev":
                        c1, c2, c3 = st.columns(3)
                        c1.metric("🔑 Kapı Kodu", srv.lockbox_code)
                        c2.metric("📶 Wi-Fi Ağı", srv.wifi_ssid)
                        c3.metric("🔒 Wi-Fi Şifresi", srv.wifi_password)
                        st.markdown(f"📍 **Açık Adres:** {srv.location}\n\n⏰ **Giriş:** {srv.checkin_time} sonrası | **Çıkış:** {srv.checkout_time}'ye kadar")
                    else:
                        c1, c2, c3 = st.columns(3)
                        c1.metric("⚓ Tekne Adı", srv.boat_name)
                        c2.metric("⏰ Seans Saati", res.selected_slot or "Günlük Tur")
                        c3.metric("👥 Kapasite", srv.capacity)
                        st.markdown(f"📍 **Buluşma Noktası:** {srv.location}\n\n🌊 **Rota:** {srv.tour_route}")

                st.markdown("---")
                st.subheader("💬 Sorularınızı Yapay Zeka Asistanımıza Sorun")
                
                if "guest_chat_history" not in st.session_state:
                    st.session_state.guest_chat_history = []

                for spk, txt, alert, atype, wa_alert_link in st.session_state.guest_chat_history:
                    if spk == "guest":
                        with st.chat_message("user"):
                            st.write(txt)
                    else:
                        with st.chat_message("assistant"):
                            st.markdown(txt)
                            if alert:
                                st.warning(f"🔔 {atype}: Durum işletme paneline anında işlendi.")
                                if wa_alert_link:
                                    st.link_button(f"📲 Durumu Yetkiliye ({srv.organizer_phone}) WhatsApp'tan Doğrudan İlet", wa_alert_link, type="primary")

                u_msg = st.chat_input("Sorunuzu yazın (Örn: '15 dk gecikeceğiz', 'Wi-Fi şifresi ne?', 'Koylar neresi?')")
                if u_msg:
                    r_txt, is_al, a_tp = portal_ai_engine(u_msg, srv, res)
                    wa_alert_url = None
                    if is_al:
                        alert_content = f"⚠️ *{a_tp}* ({srv.title})\n\nMisafir: {res.guest_name}\nMesaj: {u_msg}"
                        wa_alert_url = build_wa_url(srv.organizer_phone, alert_content)
                    
                    st.session_state.guest_chat_history.append(("guest", u_msg, False, None, None))
                    st.session_state.guest_chat_history.append(("assistant", r_txt, is_al, a_tp, wa_alert_url))
                    st.rerun()

elif active_route == "review":
    with SessionLocal() as db:
        res = db.query(Reservation).options(joinedload(Reservation.service)).filter(Reservation.feedback_token == token_review).first()
        if not res:
            st.error("❌ Geçersiz değerlendirme bağlantısı.")
        else:
            srv = res.service
            st.title("⭐ Hizmet Değerlendirmesi")
            st.markdown(f"### Sevgili **{res.guest_name}**, nasıldı?")
            st.write(f"**{srv.title}** deneyiminizi puanlayarak hizmet kalitemizi geliştirmemize yardımcı olabilirsiniz.")
            st.markdown("---")
            
            if "selected_star" not in st.session_state:
                st.session_state.selected_star = res.rating or 5
            
            star_cols = st.columns(5)
            star_labels = ["1 Yıldız", "2 Yıldız", "3 Yıldız", "4 Yıldız", "5 Yıldız"]
            for i in range(1, 6):
                with star_cols[i - 1]:
                    btn_label = f"⭐ {i}" if i <= st.session_state.selected_star else f"☆ {i}"
                    if st.button(btn_label, key=f"star_btn_{i}", use_container_width=True):
                        st.session_state.selected_star = i
                        st.rerun()
            
            st.markdown(f"**Seçilen Puan:** {'⭐' * st.session_state.selected_star} *({st.session_state.selected_star} / 5 Yıldız)*")
            st.markdown("---")
            r_note = st.text_area("Görüşleriniz ve Misafir Notunuz:", value=res.review_note or "", height=130)
            
            if st.button("Değerlendirmeyi Gönder", type="primary", use_container_width=True):
                res.rating = st.session_state.selected_star
                res.review_note = r_note
                res.review_submitted_at = datetime.now()
                db.commit()
                st.balloons()
                st.success("🎉 Değerlendirmeniz başarıyla kaydedildi! Teşekkür ederiz.")

else:
    st.title("🏡 Rezervasyon, Airbnb & WhatsApp Yönetim Paneli")
    
    with st.sidebar:
        st.markdown("### 🌐 Sistem Alan Adı / IP")
        base_host = st.text_input("Portal Adresi:", value=[https://misafir-portali.streamlit.app](https://misafir-portali.streamlit.app))
    
    tab0, tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📖 Başlangıç Rehberi",
        "📝 1. Hizmet Tanımla (Ev / Yat)", 
        "🔄 2. Airbnb Takvim Senkronizasyonu",
        "🔗 3. Manuel Rezervasyon & WhatsApp", 
        "🚨 4. Canlı Alarmlar & Gecikmeler",
        "📊 5. Gelen Değerlendirmeler"
    ])
    
    with tab0:
        st.subheader("🚀 İlan Verenler İçin Sistem Kullanım Kılavuzu")
        st.markdown("""
        Bu sistem; villa/ev kiralayan ev sahipleri ve yat/tekne turu işletmecileri için **misafir karşılama, 7/24 AI concierge asistanı ve çıkış anketlerini** tek merkezden yönetmek üzere tasarlanmıştır.

        ---

        ### 📍 1. Adım: İlanınızı Tanımlayın
        * **'1. Hizmet Tanımla'** sekmesinden Ev veya Yat turunuzu bilgileriyle kaydedin.

        ### 🔄 2. Adım: Airbnb Rezervasyonları & WhatsApp Numara Toplama
        * Airbnb takvim linkinizi tanımlayın.
        * Misafire rehber linkini iletin. Misafir **numarasını yazmadan kapı/marina bilgisine ve asistana erişemez**. Numara anında panelinize düşer.

        ### 💬 3. Adım: Manuel Rezervasyon & WhatsApp
        * Direkt gelen müşteriler için **'3. Manuel Rezervasyon'** sekmesinden tek tıkla mesaj atın.
        """)

    with tab1:
        st.subheader("Yeni Bir Hizmet Tanımlayın")
        chosen_type = st.radio("Hizmet Türü:", ["🏡 Ev / Villa Kiralama", "⛵ Özel Yat & Tekne Turu"], horizontal=True)
        st.markdown("---")
        
        with SessionLocal() as db:
            if "Ev" in chosen_type:
                c1, c2 = st.columns(2)
                with c1:
                    title = st.text_input("Ev / Villa Başlığı", "Kadıköy Moda Loft Daire")
                    loc = st.text_input("Açık Adres", "Moda Cad. No:14 D:3 Kadıköy/İstanbul")
                    org_phone = st.text_input("Ev Sahibi WhatsApp No", "05321110022")
                    ical_inp = st.text_input("Airbnb iCal URL'si (Opsiyonel)", placeholder="https://www.airbnb.com.tr/calendar/ical/...")
                    lock = st.text_input("Kapı / Kasa Şifresi", "4826")
                with c2:
                    wifi_s = st.text_input("Wi-Fi Adı", "ModaLoft_5G")
                    wifi_p = st.text_input("Wi-Fi Şifresi", "moda2026")
                    c_in = st.text_input("Giriş Saati", "15:00")
                    c_out = st.text_input("Çıkış Saati", "11:00")
                rules = st.text_area("Ev Kuralları & Rehber", "Balkon dahil sigara yasaktır. Çöpler akşam 20:00'de konteynere bırakılır.")
                
                if st.button("Ev Hizmetini Kaydet", type="primary"):
                    db.add(ServiceListing(
                        service_type="ev", title=title, location=loc, organizer_phone=org_phone,
                        ical_url=ical_inp, lockbox_code=lock, wifi_ssid=wifi_s, wifi_password=wifi_p,
                        checkin_time=c_in, checkout_time=c_out, details_and_rules=rules
                    ))
                    db.commit()
                    st.success(f"🏡 '{title}' başarıyla kaydedildi!")
            else:
                c1, c2 = st.columns(2)
                with c1:
                    title = st.text_input("Tur Başlığı", "Bodrum VIP Gün Batımı Yat Turu")
                    b_name = st.text_input("Tekne / Yat Adı", "M/Y Poseidon")
                    loc = st.text_input("Kalkış Marinası", "Bodrum Milta Marina - G İskelesi")
                    org_phone = st.text_input("Kaptan WhatsApp No", "05329998877")
                    ical_inp = st.text_input("Airbnb / Yat Takvimi iCal URL'si (Opsiyonel)", placeholder="https://www.airbnb.com.tr/calendar/ical/...")
                    cap = st.text_input("Kapasite", "8 Kişi")
                    price = st.text_input("Fiyat", "350 €")
                with c2:
                    all_days = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
                    sel_days = st.multiselect("Aktif Günler:", all_days, default=["Cuma", "Cumartesi", "Pazar"])
                    raw_slots = st.text_area("Seanslar", "10:00 - 15:00 (Öğle Seansı)\n16:30 - 20:30 (Gün Batımı Seansı)")
                    route = st.text_area("Rota & Koylar", "Akvaryum Koyu, Karaada Mağarası, Poyraz Koyu")
                rules = st.text_area("İkramlar & Kurallar", "Meyve tabağı ve meşrubatlar dahildir. Şnorkeller teknede mevcuttur.")
                
                if st.button("Yat Turunu Kaydet", type="primary"):
                    slot_list = [s.strip() for s in raw_slots.split("\n") if s.strip()]
                    db.add(ServiceListing(
                        service_type="yat", title=title, boat_name=b_name, location=loc,
                        organizer_phone=org_phone, ical_url=ical_inp, capacity=cap, price_details=price,
                        active_days=json.dumps(sel_days, ensure_ascii=False),
                        defined_slots=json.dumps(slot_list, ensure_ascii=False),
                        tour_route=route, details_and_rules=rules
                    ))
                    db.commit()
                    st.success(f"⛵ '{title}' başarıyla kaydedildi!")

    with tab2:
        st.subheader("🔄 Airbnb Takvim Senkronizasyonu & Misafir Numaraları")
        with SessionLocal() as db:
            all_services = db.query(ServiceListing).all()
            if not all_services:
                st.info("Henüz kayıtlı bir ev veya yat ilanı bulunmuyor.")
            else:
                srv_opts = {f"[{'🏡 EV' if s.service_type=='ev' else '⛵ YAT'}] {s.title}": s.id for s in all_services}
                sel_label = st.selectbox("Senkronize Edilecek Hizmeti Seçin:", list(srv_opts.keys()))
                sel_id = srv_opts[sel_label]
                sel_obj = db.query(ServiceListing).filter(ServiceListing.id == sel_id).first()
                
                st.caption(f"Tanımlı iCal Linki: `{sel_obj.ical_url or 'Henüz iCal linki girilmemiş'}`")
                
                if st.button("⚡ Airbnb Takvimini Şimdi Senkronize Et", type="primary"):
                    with st.spinner("Airbnb takvimi taranıyor..."):
                        ok, res_msg = sync_service_calendar(sel_id)
                        if ok:
                            st.success(res_msg)
                        else:
                            st.error(res_msg)
                
                st.markdown("---")
                st.markdown("### 📋 Airbnb'den İçe Aktarılan Rezervasyonlar")
                airbnb_res = db.query(Reservation).filter(
                    Reservation.service_id == sel_id,
                    Reservation.source == "Airbnb"
                ).order_by(Reservation.start_time.asc()).all()
                
                if not airbnb_res:
                    st.info("Bu ilan için henüz içe aktarılmış bir Airbnb rezervasyonu bulunmuyor.")
                else:
                    for r in airbnb_res:
                        with st.container():
                            c_a, c_b, c_c = st.columns([2, 2, 2])
                            c_a.markdown(f"**👤 {r.guest_name}**")
                            c_a.caption(f"📅 {r.start_time.strftime('%d.%m.%Y')}" + (f" | {r.selected_slot}" if r.selected_slot else ""))
                            
                            chat_url = f"{base_host}/?chat={r.access_token}"
                            rev_url = f"{base_host}/?review={r.feedback_token}"
                            
                            if r.phone_verified == "Evet":
                                c_b.success(f"✅ Tel: `{r.guest_phone}` (Doğrulandı)")
                                msg_rev = f"Sevgili {r.guest_name}, bizi tercih ettiğiniz için teşekkür ederiz! ✨ Deneyiminizi puanlayın:\n👉 {rev_url}"
                                wa_rev_url = build_wa_url(r.guest_phone, msg_rev)
                                c_c.link_button("⭐ WhatsApp'tan Çıkış Anketi Yolla", wa_rev_url, type="primary", use_container_width=True)
                            else:
                                c_b.warning("⏳ Telefon henüz girilmedi (Bekleniyor)")
                                c_c.code(chat_url, language="markdown")
                                c_c.caption("👆 Bu linki Airbnb mesaj kutusundan misafire iletin.")
                            st.divider()

    with tab3:
        st.subheader("Manuel Rezervasyon Oluştur & WhatsApp Linki")
        with SessionLocal() as db:
            services = db.query(ServiceListing).all()
            if not services:
                st.warning("Önce 1. sekmeden bir ev veya yat tanımlayın.")
            else:
                srv_map = {f"[{'🏡 EV' if s.service_type=='ev' else '⛵ YAT'}] {s.title}": s.id for s in services}
                sel_label = st.selectbox("Hizmet Seçin:", list(srv_map.keys()))
                selected_srv = db.query(ServiceListing).filter(ServiceListing.id == srv_map[sel_label]).first()
                
                c1, c2 = st.columns(2)
                with c1:
                    g_name = st.text_input("Misafir Adı Soyadı", "Burak Demir")
                    g_phone = st.text_input("Misafir Telefon Numarası", "05321112233")
                with c2:
                    if selected_srv.service_type == "ev":
                        d_in = st.date_input("Giriş Tarihi", datetime.now())
                        d_out = st.date_input("Çıkış Tarihi", datetime.now() + timedelta(days=3))
                        sel_slot = None
                    else:
                        tour_d = st.date_input("Tur Günü", datetime.now())
                        slots = json.loads(selected_srv.defined_slots) if selected_srv.defined_slots else ["10:00 - 15:00"]
                        sel_slot = st.selectbox("Seans Seçin:", slots)

                if st.button("Rezervasyonu Kaydet ve Linkleri Hazırla", type="primary"):
                    chat_tok = str(uuid.uuid4())[:8]
                    rev_tok = str(uuid.uuid4())[:8]
                    
                    if selected_srv.service_type == "ev":
                        dt_start = datetime.combine(d_in, datetime.min.time()).replace(hour=15)
                        dt_end = datetime.combine(d_out, datetime.min.time()).replace(hour=11)
                    else:
                        dt_start = datetime.combine(tour_d, datetime.min.time()).replace(hour=10)
                        dt_end = datetime.combine(tour_d, datetime.min.time()).replace(hour=15)

                    new_res = Reservation(
                        access_token=chat_tok, feedback_token=rev_tok,
                        service_id=selected_srv.id, guest_name=g_name,
                        guest_phone=g_phone, phone_verified="Evet", source="Manuel", selected_slot=sel_slot,
                        start_time=dt_start, end_time=dt_end
                    )
                    db.add(new_res)
                    db.commit()
                    
                    chat_url = f"{base_host}/?chat={chat_tok}"
                    rev_url = f"{base_host}/?review={rev_tok}"
                    
                    msg_1 = f"Merhaba {g_name}, {selected_srv.title} rezervasyonunuz onaylandı! 🌟\n\nGiriş/tur rehberinize ve 7/24 Yapay Zeka Asistanınıza buradan ulaşabilirsiniz:\n👉 {chat_url}"
                    msg_2 = f"Sevgili {g_name}, bizi tercih ettiğiniz için teşekkür ederiz! ✨\n\nDeneyiminizi yıldızlarla puanlamak için değerlendirme formumuz:\n👉 {rev_url}"
                    
                    wa_url_1 = build_wa_url(g_phone, msg_1)
                    wa_url_2 = build_wa_url(g_phone, msg_2)
                    clean_formatted_phone = sanitize_phone_number(g_phone)
                    
                    st.success(f"🎉 {g_name} adına rezervasyon kaydedildi! (Numara: +{clean_formatted_phone})")
                    st.markdown("---")
                    
                    col_wa1, col_wa2 = st.columns(2)
                    with col_wa1:
                        st.markdown("#### 1. Giriş Mesajı (AI Asistan Linki)")
                        st.code(msg_1, language="markdown")
                        st.link_button("📲 WhatsApp'tan 1. Mesajı Gönder", wa_url_1, type="primary", use_container_width=True)
                    
                    with col_wa2:
                        st.markdown("#### 2. Çıkış Mesajı (Yıldızlı Anket Linki)")
                        st.code(msg_2, language="markdown")
                        st.link_button("📲 WhatsApp'tan 2. Mesajı Gönder", wa_url_2, use_container_width=True)

    with tab4:
        st.subheader("🚨 Canlı Gecikme & Acil Durum Bildirimleri")
        with SessionLocal() as db:
            alerts = db.query(AlertLog).options(joinedload(AlertLog.service), joinedload(AlertLog.reservation)).order_by(AlertLog.id.desc()).all()
            if not alerts:
                st.info("Henüz iletilen bir gecikme veya acil durum bildirimi yok.")
            else:
                for alt in alerts:
                    st.error(f"**{alt.alert_type}** | 👤 Misafir: **{alt.reservation.guest_name}** ({alt.service.title})")
                    st.write(f"💬 *\"{alt.message}\"*")
                    st.caption(f"Tarih / Saat: {alt.created_at.strftime('%d.%m.%Y %H:%M')}")
                    reply_wa = build_wa_url(alt.reservation.guest_phone, f"Merhaba {alt.reservation.guest_name}, bildiriminiz hakkında ulaşıyorum...")
                    st.link_button(f"📲 {alt.reservation.guest_name} Kişisine WhatsApp'tan Yaz", reply_wa)
                    st.divider()

    with tab5:
        st.subheader("📊 Misafir Değerlendirmeleri ve Puanları")
        with SessionLocal() as db:
            reviews = db.query(Reservation).options(joinedload(Reservation.service)).filter(Reservation.rating.isnot(None)).all()
            if not reviews:
                st.info("Henüz değerlendirme gönderen misafir bulunmuyor.")
            else:
                for r in reviews:
                    st.markdown(f"**👤 {r.guest_name}** ({r.service.title}) — {'⭐' * r.rating} *({r.rating}/5 Yıldız)*")
                    st.write(f"💬 *\"{r.review_note or 'Yorum yazılmadı.'}\"*")
                    st.caption(f"Tarih: {r.review_submitted_at.strftime('%d.%m.%Y %H:%M') if r.review_submitted_at else '-'}")
                    st.divider()
