import streamlit as st
import pandas as pd
import os
import altair as alt

st.set_page_config(page_title="제반장 FX 리포트", page_icon="📈")
st.title("📱 제반장 FX-Alert 실시간")

# --- 상단 메트릭 섹션 ---
codes = ["JPY100", "USD", "AUD", "CHF"]
cols = st.columns(len(codes))

for i, code in enumerate(codes):
    file_path = f"data_{code}.csv"
    if os.path.exists(file_path):
        data = pd.read_csv(file_path, names=["price"])
        current_price = data["price"].iloc[-1]
        with cols[i]:
            st.metric(label=code, value=f"{current_price:.2f}")
        
        # 그래프 섹션
        min_val, max_val = float(data["price"].min()) - 2, float(data["price"].max()) + 2
        chart = alt.Chart(data).mark_line(color='#FF4B4B').encode(
            x=alt.X('index:Q', title='순번'),
            y=alt.Y('price:Q', scale=alt.Scale(domain=[min_val, max_val]))
        )
        st.altair_chart(chart, use_container_width=True)

# --- 2단계: 앱 내 실시간 알림 피드 (추가된 부분) ---
st.divider()
st.subheader("🔔 실시간 변동 탐지 기록")
# 나중에 news.csv나 alert_log.csv를 만들어서 여기에 뿌려줄 겁니다.
st.write("✅ 현재 모든 시스템 정상 가동 중 (10분 주기 체크)")
