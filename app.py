import streamlit as st
import pandas as pd
import os

st.set_page_config(page_title="제반장 FX 리포트", page_icon="📈")
st.title("📱 제반장 FX-Alert 실시간")

codes = ["JPY100", "USD", "AUD", "CHF"]
cols = st.columns(len(codes))

for i, code in enumerate(codes):
    file_path = f"data_{code}.csv"
    if os.path.exists(file_path):
        data = pd.read_csv(file_path, names=["price"])
        current_price = data["price"].iloc[-1]
        
        # 스케일 계산: 최소값 - 10, 최대값 + 10
        min_val = float(data["price"].min()) - 10
        max_val = float(data["price"].max()) + 10
        
        with cols[i]:
            st.metric(label=code, value=f"{current_price:.2f}")
        
        st.subheader(f"📊 {code} 차트 (집중 모드)")
        
        # Y축 범위를 지정하여 차트 생성
        st.line_chart(data, y_label="가격", use_container_width=True, 
                      y_configs={"price": {"min": min_val, "max": max_val}}) 
        # 주의: Streamlit 버전에 따라 y_configs 대신 아래 방식이 더 확실할 수 있습니다.
        # st.area_chart(data) 대신 line_chart를 쓰되, 
        # 최신 버전은 자동으로 범위를 잡아주지만, 수동 설정은 아래 st.altair_chart가 정확합니다.
        
    else:
        st.info(f"{code} 수집 전")
