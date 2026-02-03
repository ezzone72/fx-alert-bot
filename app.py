import streamlit as st
import pandas as pd
import os
import altair as alt

st.set_page_config(page_title="제반장 FX 리포트", page_icon="📈", layout="wide")
st.title("📱 제반장 FX-Alert 실시간")

codes = ["JPY100", "USD", "AUD", "CHF"]
cols = st.columns(len(codes))

for i, code in enumerate(codes):
    file_path = f"data_{code}.csv"
    if os.path.exists(file_path):
        # 1. 데이터 불러오기
        data = pd.read_csv(file_path, names=["price"])
        data = data.reset_index() # x축을 위한 인덱스 생성
        current_price = data["price"].iloc[-1]
        
        # 2. 상단 숫자 표시
        with cols[i]:
            st.metric(label=code, value=f"{current_price:.2f}")
        
        # 3. 그래프 범위 설정 (데이터의 최소/최대값 기준)
        min_val = float(data["price"].min()) - 1
        max_val = float(data["price"].max()) + 1
        
        # 4. 진짜 그래프 그리기 (Altair 버전)
        chart = alt.Chart(data).mark_line(
            color='#FF4B4B',
            point=True # 데이터 점도 찍어줍니다
        ).encode(
            x=alt.X('index:Q', title='최근 데이터 순서'),
            y=alt.Y('price:Q', title='가격(원)', scale=alt.Scale(domain=[min_val, max_val])),
            tooltip=['index', 'price'] # 마우스 올리면 값 보이게
        ).properties(
            height=300 # 그래프 높이 조절
        ).interactive()
        
        st.subheader(f"📊 {code} 흐름")
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info(f"{code} 수집 중...")

st.divider()
st.caption("알림 설정: 10분 주기 자동 갱신 중 | ExpertAlpha-K100")
