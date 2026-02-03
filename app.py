import streamlit as st
import pandas as pd
import os

st.set_page_config(page_title="제반장 FX 리포트", page_icon="📈")
st.title("📱 제반장 FX-Alert 실시간")

# 깔끔하게 한 줄씩 출력
codes = ["JPY100", "USD", "AUD", "CHF"]
cols = st.columns(len(codes)) # 화면을 4칸으로 나눔

for i, code in enumerate(codes):
    file_path = f"data_{code}.csv"
    if os.path.exists(file_path):
        data = pd.read_csv(file_path, names=["price"])
        current_price = data["price"].iloc[-1]
        
        with cols[i]:
            st.metric(label=code, value=f"{current_price:.2f}") # 소수점 2자리만 깔끔하게!
        
        st.subheader(f"📊 {code} 차트")
        st.line_chart(data)
    else:
        st.info(f"{code} 수집 전")
