import streamlit as st
import pandas as pd
import os
import altair as alt

# 앱처럼 보이게 하는 설정 (주소창/메뉴 최소화 준비)
st.set_page_config(
    page_title="ExpertAlpha-K100", 
    page_icon="💹", 
    layout="centered", # 앱처럼 가운데 정렬
    initial_sidebar_state="collapsed" # 메뉴바 숨기기
)

# 스마트폰 전용 폰트 크기 및 스타일 조절 (CSS)
st.markdown("""
    <style>
    .main { background-color: #f5f7f9; }
    .stMetric { background-color: #ffffff; padding: 15px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    [data-testid="stHeader"] { visibility: hidden; } /* 상단 헤더 숨김 */
    </style>
    """, unsafe_allow_label=True)

st.title("💹 제반장 FX-Alert")
