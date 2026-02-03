import streamlit as st
import pandas as pd
import os

st.title("📱 제반장 FX-Alert 리포트")

# 저장된 환율 CSV 파일들을 불러와서 보여줌
for code in ["JPY100", "USD", "AUD", "CHF"]:
    file_path = f"data_{code}.csv"
    if os.path.exists(file_path):
        data = pd.read_csv(file_path, names=["환율"])
        st.subheader(f"📊 {code} 흐름")
        st.line_chart(data) # 그래프 그리기
        st.write(f"현재가: {data['환율'].iloc[-1]}")
    else:
        st.info(f"{code} 데이터 수집 중...")
