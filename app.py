import streamlit as st
import pandas as pd
import os
import altair as alt

st.set_page_config(page_title="제반장 FX 리포트", page_icon="📈")
st.title("📱 제반장 FX-Alert 실시간")

codes = ["JPY100", "USD", "AUD", "CHF"]
cols = st.columns(len(codes))

for i, code in enumerate(codes):
    file_path = f"data_{code}.csv"
    if os.path.exists(file_path):
        # 헤더가 없으므로 names로 컬럼명을 지정
        data = pd.read_csv(file_path, names=["price"])
        data = data.reset_index() # 인덱스를 시간 대용으로 사용
        current_price = data["price"].iloc[-1]
        
        # 스케일 계산: 데이터의 최소/최대값에서 ±2원만 여유를 둡니다 (10원은 너무 멀 수 있음)
        min_val = float(data["price"].min()) - 2
        max_val = float(data["price"].max()) + 2
        
        with cols[i]:
            st.metric(label=code, value=f"{current_price:.2f}")
        
        st.subheader(f"📊 {code} 집중 차트")
        
        # 0원을 표시하지 않고 데이터 범위만 보여주는 정석 코드
        chart = alt.Chart(data).mark_line(color='#FF4B4B').encode(
            x=alt.X('index:Q', title='시간(순번)'),
            y=alt.Y('price:Q', title='가격', scale=alt.Scale(domain=[min_val, max_val]))
        ).interactive()
        
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info(f"{code} 수집 전")
