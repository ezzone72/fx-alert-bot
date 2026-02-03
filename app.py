import streamlit as st
import pandas as pd
import os
import altair as alt

# 앱 느낌 나도록 설정
st.set_page_config(
    page_title="제반장 FX", 
    page_icon="💹", 
    layout="centered"
)

# CSS로 디자인 다듬기 (오타 수정 완료!)
st.markdown("""
    <style>
    [data-testid="stHeader"] { visibility: hidden; } /* 상단 메뉴 숨김 */
    .main { background-color: #f9f9f9; }
    </style>
    """, unsafe_allow_html=True)

st.title("💹 제반장 FX-Alert")

codes = ["JPY100", "USD", "AUD", "CHF"]
cols = st.columns(len(codes))

for i, code in enumerate(codes):
    file_path = f"data_{code}.csv"
    if os.path.exists(file_path):
        data = pd.read_csv(file_path, names=["price"])
        data = data.reset_index()
        current_price = data["price"].iloc[-1]
        
        with cols[i]:
            st.metric(label=code, value=f"{current_price:.2f}")
        
        min_val = float(data["price"].min()) - 1
        max_val = float(data["price"].max()) + 1
        
        chart = alt.Chart(data).mark_line(color='#FF4B4B', point=True).encode(
            x=alt.X('index:Q', title=None),
            y=alt.Y('price:Q', title=None, scale=alt.Scale(domain=[min_val, max_val]))
        ).properties(height=200).interactive()
        
        st.write(f"**{code} 추이**")
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info(f"{code} 대기중")

st.divider()
st.caption("ExpertAlpha-K100 | 10분 주기 자동 업데이트")
