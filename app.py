import streamlit as st
import pandas as pd
import os
import altair as alt

# 1. 폰 화면에 딱 맞게 레이아웃 설정
st.set_page_config(page_title="제반장 FX", page_icon="💹", layout="centered")

# 2. 제목 크기 줄이고 여백 조절 (CSS)
st.markdown("""
    <style>
    [data-testid="stHeader"] { visibility: hidden; }
    h1 { font-size: 1.5rem !important; color: #333; } /* 제목 크기 축소 */
    .stMetric { padding: 5px !important; } /* 간격 축소 */
    </style>
    """, unsafe_allow_html=True)

st.title("💹 제반장 FX-Alert")

codes = ["JPY100", "USD", "AUD", "CHF"]

# 폰에서는 4칸을 다 쓰면 좁아서 제목이 깨지므로, 2개씩 배치하거나 한 줄씩 보여줍니다.
for code in codes:
    file_path = f"data_{code}.csv"
    if os.path.exists(file_path):
        # 데이터 읽기 및 전처리
        data = pd.read_csv(file_path, names=["price"])
        
        # CHF 등이 안 나오는 걸 방지하기 위해 강제로 숫자형 변환
        data["price"] = pd.to_numeric(data["price"], errors='coerce')
        data = data.dropna() # 빈 값 제거
        data = data.reset_index()
        
        if len(data) > 0:
            current_price = data["price"].iloc[-1]
            
            # 메트릭 표시
            st.metric(label=f"{code} 현재가", value=f"{current_price:.2f}")
            
            # 그래프 범위 계산 (데이터가 1개일 때를 대비해 여유값 설정)
            min_val = float(data["price"].min()) - 1
            max_val = float(data["price"].max()) + 1
            
            # 폰 화면용 그래프 (심플하게)
            chart = alt.Chart(data).mark_line(color='#FF4B4B', point=True).encode(
                x=alt.X('index:Q', title=None),
                y=alt.Y('price:Q', title=None, scale=alt.Scale(domain=[min_val, max_val]))
            ).properties(height=150).interactive()
            
            st.altair_chart(chart, use_container_width=True)
            st.divider()
        else:
            st.info(f"{code}: 데이터 분석 중...")
    else:
        st.info(f"{code}: 데이터 수집 전")

st.caption("ExpertAlpha-K100 | 10분 주기 업데이트")
