import pandas as pd
import requests
import time
import re
import os
import math
import uuid
import glob
import hashlib
from typing import Optional, Any, List
import json
import base64
import urllib.parse
import streamlit as st
import streamlit.components.v1 as components
import concurrent.futures
from datetime import datetime, timedelta
from dotenv import load_dotenv
from functools import lru_cache
import extra_streamlit_components as stx
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_classic.output_parsers import OutputFixingParser
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openai import ChatOpenAI
from langchain_classic.chains import create_retrieval_chain
from langchain_classic.chains.combine_documents import create_stuff_documents_chain
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from pydantic import BaseModel, Field
from langchain_core.output_parsers import PydanticOutputParser

# 쿠키 매니저
def get_cookie_manager():
    return stx.CookieManager(key="sm_cookies")

cookie_manager = get_cookie_manager()

# ==============================++
# 다중 대화방 & 계정(회원가입) 관리 엔진
# ==============================++
BASE_SESSION_DIR = "./seoulmate_sessions"
USER_DB_FILE = "./seoulmate_users.json" # 실제 유저 정보가 저장될 DB 파일

# 24시간 지난 게스트 파일 자동 삭제
def cleanup_guest_sessions():
    guest_dir = os.path.join(BASE_SESSION_DIR, "guest")
    if not os.path.exists(guest_dir): return
    
    current_time = time.time()
    # guest 폴더 안의 모든 json 파일을 검사
    for file_path in glob.glob(os.path.join(guest_dir, "*.json")):
        # 파일이 마지막으로 수정된 시간(getmtime)이 현재 시간보다 24시간(86400초) 이상 과거라면
        if os.path.getmtime(file_path) < current_time - 86400:
            try:
                os.remove(file_path) 
            except Exception:
                pass

cleanup_guest_sessions()

def load_users():
    """가입된 유저 목록을 불러옵니다."""
    if not os.path.exists(USER_DB_FILE): return {}
    with open(USER_DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(users_data):
    """새로운 유저 정보를 파일에 저장합니다."""
    with open(USER_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(users_data, f, ensure_ascii=False)

def hash_pw(password):
    """보안을 위해 비밀번호를 해시(암호화) 처리합니다."""
    return hashlib.sha256(password.encode()).hexdigest()

def validate_password(password):
    """비밀번호 보안 규칙 검사"""
    if len(password) < 8:
        return False, "비밀번호는 최소 8자 이상이어야 합니다."
    if not any(c.islower() for c in password):
        return False, "비밀번호에 영소문자가 포함되어야 합니다."
    if not any(c.isdigit() for c in password):
        return False, "비밀번호에 숫자가 포함되어야 합니다."
    if not any(not c.isalnum() for c in password):
        return False, "비밀번호에 특수기호(!@#$%^&*)가 포함되어야 합니다."
    return True, "성공"

def register_user(username, password, nickname):
    """회원가입 로직 (아이디 및 닉네임 중복 검사)"""
    users = load_users()
    if username in users: 
        return "id_taken" # 이미 존재하는 아이디
    
    # 닉네임 중복 체크
    for uid, info in users.items():
        if info.get("nickname") == nickname:
            return "nickname_taken"
    
    # "admin"이라는 아이디로 가입하면 무조건 자동 승인 (최고 관리자용)
    user_status = "approved" if username == "admin" else "pending"

    users[username] = {
        "pw_hash": hash_pw(password), 
        "nickname": nickname,          # 닉네임 저장
        "status": user_status,         # 가입 상태 (pending or approved)
        "created_at": str(datetime.now())
    }
    save_users(users)
    return "success"

def check_login(username, password):
    """로그인 검증 로직"""
    users = load_users()
    if username not in users: 
        return False, "존재하지 않는 아이디입니다.", None
    if users[username]["pw_hash"] != hash_pw(password):
        return False, "비밀번호가 일치하지 않습니다.", None
    if users[username].get("status", "approved") == "pending":
        return False, "⏳ 관리자 승인 대기 중입니다. 관리자에게 문의하세요.", None
    
    # 성공 시 : (True, 성공메시지, 닉네임)
    return True, "성공", users[username].get("nickname", username)

# 관리자가 대기 중인 유저를 승인하는 함수
def approve_user(target_username):
    users = load_users()
    if target_username in users:
        users[target_username]["status"] = "approved"
        save_users(users)
        return True
    return False

# 관리자가 대기 중인 유저를 거절(삭제)하는 함수
def reject_user(target_username):
    users = load_users()
    if target_username in users:
        # DB에서 해당 유저 데이터를 완전히 삭제합
        del users[target_username]
        save_users(users)
        return True
    return False

def get_user_dir():
    """현재 로그인한 사용자의 전용 저장 폴더 경로를 반환합니다. (게스트 포함)"""
    username = st.session_state.get("username", "guest")
    
    user_dir = os.path.join(BASE_SESSION_DIR, username)
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def reset_search_context():
    """사용자가 검색 모드를 끝내고 일정 생성 모드로 넘어갈 때 필터를 초기화합니다."""
    st.session_state.shown_hotels_dict = {}
    st.session_state.shown_places_dict = {}
    if "all_shown_hotels" in st.session_state:
        del st.session_state["all_shown_hotels"]
    if "all_shown_places" in st.session_state:
        del st.session_state["all_shown_places"]

def save_current_room():
    """현재 방의 모든 대화와 상태를 파일로 저장합니다."""
    if "current_room_id" not in st.session_state: return
    
    user_dir = get_user_dir()

    room_id = st.session_state.current_room_id
    
    state_keys = ["step", "trip_info", "show_planner_button", "user_preferences", 
                  "acco_conditions", "shown_hotels_dict", "bad_weather_details", 
                  "indoor_preference", "weather_summary", "acco_type", "selected_hotel_name", "travel_companion",
                  "place_conditions", "travel_style", "shown_places_dict", "all_shown_places", "all_shown_hotels"]
    
    data = {}
    for k in state_keys:
        if k in st.session_state:
            val = st.session_state[k]
            if isinstance(val, set):
                data[k] = list(val)
            else:
                data[k] = val
    
    if "memory" in st.session_state:
        data["messages"] = [{"role": "user" if m.type == "human" else "ai", "content": m.content} 
                            for m in st.session_state.memory.messages]
    
    if "custom_room_title" in st.session_state:
        data["room_title"] = st.session_state.custom_room_title
    else:
        first_user_msg = next((m['content'] for m in data.get("messages", []) if m['role'] == 'user'), None)
        if first_user_msg:
            auto_title = first_user_msg[:15] + "..." if len(first_user_msg) > 15 else first_user_msg
            data["room_title"] = auto_title
            st.session_state.custom_room_title = auto_title 
        else:
            return 
    
    # 해당 유저의 전용 폴더에만 파일 저장
    with open(os.path.join(user_dir, f"{room_id}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def load_room(room_id):
    """파일에서 상태를 읽어와 화면을 복구합니다."""
    user_dir = get_user_dir()
    if not user_dir: return False

    file_path = os.path.join(user_dir, f"{room_id}.json")
    if not os.path.exists(file_path): return False
    
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    for k, v in data.items():
        if k != "messages" and k != "room_title":
            if k in ["all_shown_places", "all_shown_hotels"]:
                st.session_state[k] = set(v) if v else set()
            else:
                st.session_state[k] = v
            
    if "room_title" in data:
        st.session_state.custom_room_title = data["room_title"]
            
    st.session_state.memory = ChatMessageHistory()
    for msg in data.get("messages", []):
        if msg['role'] == 'user': st.session_state.memory.add_user_message(msg['content'])
        else: st.session_state.memory.add_ai_message(msg['content'])
        
    st.session_state.current_room_id = room_id
    return True

st.set_page_config(page_title="SeoulMate", page_icon="🏙️", layout="centered")

# 초기 상태 설정
if "username" not in st.session_state:
    st.session_state.username = "guest"
    st.session_state.explicit_logout = False
    st.session_state.focus_chat = True

# 브라우저의 모든 쿠키를 한 번에 긁어옴
all_cookies = cookie_manager.get_all()

# 명시적 로그아웃 (즉시 폭파)
if st.session_state.get('do_logout', False):
    st.session_state.do_logout = False
    st.session_state.explicit_logout = True
    st.session_state.username = "guest"
    st.session_state.focus_chat = True
    
    components.html(
        """
        <script>
        document.cookie = "sm_user=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
        document.cookie = "sm_nick=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
        window.location.reload();
        </script>
        """, height=0, width=0
    )
    st.stop()

# 로그인 & 생명 연장 (2시간)
# 로그인 직후뿐만 아니라, 이미 로그인된 사용자가 행동을 할 때마다 아래 로직이 돌면서 수명을 연장
elif st.session_state.get('do_login', False) or (st.session_state.get("username") != "guest"):
    if st.session_state.get('do_login', False):
        st.session_state.focus_chat = True
    st.session_state.do_login = False
    st.session_state.explicit_logout = False 
    
    u_val = st.session_state.username
    n_val = urllib.parse.quote(st.session_state.get("nickname", u_val))
    
    components.html(
        f"""
        <script>
        // 쿠키 굽기
        document.cookie = "sm_user={u_val}; max-age=7200; path=/;";
        document.cookie = "sm_nick=" + decodeURIComponent("{n_val}") + "; max-age=7200; path=/;";
        </script>
        """, height=0, width=0
    )

# F5 새로고침
else:
    if not st.session_state.get('explicit_logout', False) and st.session_state.username == "guest":
        saved_u = all_cookies.get('sm_user')
        if saved_u and saved_u not in ['', 'guest']: 
            st.session_state.username = saved_u
            st.session_state.nickname = all_cookies.get('sm_nick', saved_u)
            
            # 방 꼬임 방지
            if "current_room_id" in st.session_state: del st.session_state["current_room_id"]
            if "step" in st.session_state: del st.session_state["step"]
            st.rerun()

# 새로고침 방어 및 사이드바 라우팅
query_params = st.query_params

# 새로고침 방어 및 사이드바 라우팅
query_params = st.query_params
room_id_in_url = query_params.get("room", None)

# 로그인 전용 팝업창
@st.dialog("🔒 로그인")
def show_login_dialog():
    with st.form("login_form"):
        l_id = st.text_input("아이디", key="login_id")
        l_pw = st.text_input("비밀번호", type="password", key="login_pw")
        if st.form_submit_button("로그인", use_container_width=True):
            success, msg, nickname = check_login(l_id, l_pw)
            if success:
                # 명의를 바꾸기 전에, 기존 게스트 시절의 파일 경로를 추적
                old_room_id = st.session_state.get("current_room_id")
                guest_file_path = os.path.join(BASE_SESSION_DIR, "guest", f"{old_room_id}.json") if old_room_id else None

                # 신분 변경 (guest -> 로그인 유저)
                st.session_state.username = l_id
                st.session_state.nickname = nickname
                
                # 방 번호를 유지한 채로 저장
                save_current_room()
                
                # 내 폴더로 저장이 끝났으니, 게스트 폴더에 남은 껍데기 파일은 즉시 파기
                if guest_file_path and os.path.exists(guest_file_path):
                    try:
                        os.remove(guest_file_path)
                    except Exception:
                        pass

                st.session_state.do_login = True
                st.rerun()
            else:
                st.error(msg)

# 회원가입 전용 팝업창
@st.dialog("📝 회원가입")
def show_signup_dialog():
    with st.form("signup_form"):
        s_id = st.text_input("아이디", key="signup_id")
        s_nickname = st.text_input("닉네임", key="signup_nickname")
        s_pw = st.text_input("비밀번호", type="password", key="signup_pw")
        s_pw_check = st.text_input("비밀번호 확인", type="password", key="signup_pw_check")
        
        if st.form_submit_button("가입 신청하기", use_container_width=True):
            if not s_id or not s_pw or not s_nickname:
                st.error("모든 항목을 입력해 주세요.")
            elif s_pw != s_pw_check:
                st.error("비밀번호가 일치하지 않습니다.")
            else:
                # 비밀번호 보안 규칙 검사
                is_valid, v_msg = validate_password(s_pw)
                if not is_valid:
                    st.error(v_msg)
                else:
                    result = register_user(s_id, s_pw, s_nickname)
                    
                    if result == "success":
                        if s_id == "admin":
                            st.success("👑 최고 관리자 계정 생성 완료!")
                            st.toast("관리자 계정이 생성되었습니다. 로그인해 주세요.")
                        else:
                            st.success("🎉 가입 신청 완료! 관리자 승인 후 로그인 가능합니다.")
                            st.toast("가입 신청이 접수되었습니다.")
                        # 성공 시 3초 후 팝업이 닫힘
                        time.sleep(3)
                        st.rerun()
                    elif result == "id_taken":
                        st.error("🚨 이미 사용 중인 아이디입니다.")
                    elif result == "nickname_taken":
                        st.error("🚨 이미 사용 중인 닉네임입니다.")

with st.sidebar:
    is_admin = (st.session_state.username == "admin")
    chat_list_height = 785 if is_admin else 1050

    st.markdown("""
        <style>
        /* 1. 메인 화면 가로 넓이 확장 (데스크탑 브라우저 최적화) */
        div[data-testid="stMainBlockContainer"], 
        div[data-testid="block-container"] {
            max-width: 980px !important; /* 이 숫자를 조절하여 넓이를 마음대로 세팅할 수 있음 */
            padding-top: 0rem !important; 
            overflow: visible !important; 
        }
        
        div[data-testid="stMain"] {
            overflow-x: hidden !important; /* 혹시 모를 가로 스크롤 완벽 차단 */
        }

        /* 2. 상단 헤더 컨테이너 (고정 해제, 스크롤 따라 자연스럽게 올라감) */
        div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] #sticky-header-anchor) {
            position: relative !important; /* 고정(sticky) 완벽 해제 */
            z-index: 990 !important; 
            
            background-color: transparent !important;
            border-radius: 0px !important; 
            border: none !important;
            box-shadow: none !important;
            
            padding: 35px 20px 25px 20px !important;
            margin-top: 0px !important;
            margin-bottom: 20px !important;
            width: 100% !important;
        }

        /* 3. 타이틀과 채팅방 이름이 위아래로 예쁘게 정렬되도록 속성 추가 */
        div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] #sticky-header-anchor) [data-testid="column"]:nth-of-type(2) {
            flex-direction: column;
            justify-content: center;
        }

        /* 4. 사이드바 전체 버튼 폰트 및 패딩 조절 */
        div[data-testid="stSidebar"] .stButton button {
            font-size: 12px !important; padding: 2px 8px !important;
            min-height: 24px !important; height: 28px !important; line-height: 1.2 !important;
        }

        /* 5. 로그아웃 버튼을 오른쪽으로 정렬하기 위한 컨테이너 설정 */
        div.stButton:has(button[key*="logout"]) {
            display: flex;
            justify-content: flex-end;
        }

        /* 6. 가입 승인 대기열 텍스트 수직 정렬 */
        .pending-user-text {
            font-size: 13px;
            color: #E5E5EA;
            line-height: 28px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        /* 7. 사이드바 배경 투명화 및 유리 질감(Glassmorphism) 효과 */
        section[data-testid="stSidebar"] { 
            background-color: rgba(28, 28, 30, 0.4) !important; /* 배경을 어둡고 투명하게 조절 */
            backdrop-filter: blur(15px) !important; /* 뒷배경이 뿌옇게 비치도록 블러 처리 */
            -webkit-backdrop-filter: blur(15px) !important;
            border-right: 1px solid rgba(255, 255, 255, 0.1) !important; /* 경계선을 은은하게 */
        }
        
        /* 8. 사이드바 자체의 전체 스크롤바 생성 방지 */
        section[data-testid="stSidebar"] > div {
            overflow-y: hidden !important;
        }
        
        /* 9. [+ 새 채팅] 버튼 - 상단 Clear 버튼과 동일한 컴팩트 스타일 */
        div[data-testid="stSidebar"] div.stButton > button[kind="primary"] {
            background-color: transparent !important;
            color: #0A84FF !important;
            border: 1px solid rgba(10, 132, 255, 0.5) !important;
            border-radius: 10px !important;
            padding: 4px 12px !important;
            min-height: auto !important;
            height: auto !important;
            line-height: 1.2 !important;
            font-size: 15px !important;
            font-weight: 600 !important;
            width: auto !important;
            margin-bottom: 10px;
        }

        /* 10. 채팅방 목록의 버튼들 (수평 맞춤 및 회색 박스 제거) */
        div[data-testid="stSidebar"] div[data-testid="column"]:nth-of-type(1) button {
            background-color: transparent !important; 
            border: none !important; 
            box-shadow: none !important;
            width: 100% !important;
            padding: 0px !important;
            min-height: 32px !important;
            height: 32px !important;
            justify-content: flex-start !important;
            text-align: left !important;
            color: #E5E5EA !important;
            font-size: 15px !important;
        }

        /* 11. 선택된 방 버튼에 대한 추가 스타일 */
        div[data-testid="stSidebar"] div[data-testid="column"]:nth-of-type(1) button:active,
        div[data-testid="stSidebar"] div[data-testid="column"]:nth-of-type(1) button:focus {
            color: #FFFFFF !important;
        }

        /* 12. 아이콘 버튼(연필, 휴지통)은 중앙 정렬 유지 및 박스 제거 */
        div[data-testid="stSidebar"] div[data-testid="column"]:nth-of-type(2) button,
        div[data-testid="stSidebar"] div[data-testid="column"]:nth-of-type(3) button {
            background-color: transparent !important;
            border: none !important;
            box-shadow: none !important;
            justify-content: center !important;
            padding: 0px !important;
            min-height: 32px !important;
            height: 32px !important;
        }
        
        /* 13. 채팅 목록 사이의 간격 조절 */
        div[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] {
            margin-bottom: -12px !important; 
        }
        
        /* 14. 하단 계정 정보 상자를 화면 맨 아래에 완벽 고정 */
        div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] #sidebar-bottom-anchor) {
            position: absolute !important;
            bottom: 0px !important;
            background: linear-gradient(to top, rgba(28,28,30,1) 40%, rgba(28,28,30,0) 100%) !important;
            padding: 30px 0px 20px 0px !important;
            margin-bottom: -20px !important; /* 하단 여백 초기화 */
            z-index: 999 !important;
        }

        /* 15. 관리자용 헤더 */
        .admin-header {
            font-size: 18px !important;
            color: #FFFFFF !important;
            font-weight: 800 !important;
            margin-top: 5px !important;
            margin-bottom: 12px !important;
            padding-left: 5px;
        }

        .pending-empty-text {
            color: #AEAEB2;
            font-size: 12px;
            margin-bottom: 15px;
            padding-left: 5px;
        }
            
        /* 16. 하단 버튼 글자 크기 및 줄바꿈 방지 */
        div.sidebar-bottom-container div[data-testid="column"] .stButton button {
            font-size: 12px !important;
            padding: 0px 4px !important;
            min-height: 28px !important;
            height: 28px !important;
            line-height: 1 !important;
            border-radius: 6px !important;
            white-space: nowrap !important;
        }

        /* 17. 로그아웃 버튼 전용 우측 정렬 */
        div.sidebar-bottom-container div[data-testid="column"]:nth-of-type(2) .stButton {
            display: flex;
            justify-content: flex-end;
        }
                
        /* 18. 포커스 시 발생하는 빨간색/주황색 테두리 및 텍스트 변화 완전 제거 */
        button:focus, button:active, div:focus, *:focus {
            outline: none !important;
            box-shadow: none !important;
            color: inherit !important; 
        }

        /* 19. 채팅방 목록 버튼이 포커스 되어도 빨갛게 변하지 않도록 고정 */
        div[data-testid="stSidebar"] div[data-testid="column"] button:focus {
            color: #E5E5EA !important;
        }

        /* 사이드바 내 모든 컨테이너(채팅목록 등)의 가로 스크롤 원천 차단 */
        section[data-testid="stSidebar"] div[style*="overflow-y"],
        section[data-testid="stSidebar"] div[style*="overflow: auto"],
        section[data-testid="stSidebar"] div[data-testid="stVerticalBlockBorderWrapper"],
        section[data-testid="stSidebar"] div[class*="stScrollArea"] {
            overflow-x: hidden !important;
        }
        
        /* 웹킷(크롬/사파리) 및 파이어폭스 가로 스크롤바 UI 물리적 멸종 */
        section[data-testid="stSidebar"] * {
            scrollbar-width: none !important;
        }
        section[data-testid="stSidebar"] *::-webkit-scrollbar:horizontal {
            display: none !important;
            height: 0px !important;
            width: 0px !important;
        }
        
        /* 하단 고정 박스가 허공에 뜨지 않도록 사이드바 내부 높이를 화면(100vh)에 꽉 차게 강제 설정 */
        section[data-testid="stSidebar"] > div:first-child {
            min-height: 100vh !important;
        }

        /* [관리자 4] 관리자 승인 대기 목록 닉네임 크기 확대 */
        .pending-user-text {
            font-size: 15px !important;
            font-weight: 600 !important;
            color: #E5E5EA !important;
            line-height: 24px !important;
            padding-left: 5px !important;
        }

        /* [관리자 4] 승인/거절 컨테이너 가로 정렬 강제 및 컴팩트 박스화 */
        div[data-testid="stHorizontalBlock"]:has(.admin-row-marker) {
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: center !important;
            margin-bottom: 0px !important; /* 간격 조절 초기화 */
        }
        div[data-testid="stHorizontalBlock"]:has(.admin-row-marker) div[data-testid="column"] {
            min-width: 0 !important;
            width: auto !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.admin-row-marker) button {
            font-size: 11px !important;
            padding: 2px 6px !important;
            min-height: 24px !important;
            height: 24px !important;
            line-height: 1 !important;
            border-radius: 6px !important;
            border: 1px solid rgba(255,255,255,0.2) !important; /* 컴팩트 박스선 */
            background-color: transparent !important;
            color: #E5E5EA !important;
        }

        /* 버튼 내부의 p 태그에도 강제 한 줄 속성 부여 */
        div[data-testid="stHorizontalBlock"]:has(.admin-row-marker) button p {
            white-space: nowrap !important;
            word-break: keep-all !important;
            margin: 0 !important;
        }
                
        div[data-testid="stHorizontalBlock"]:has(.admin-row-marker) button:hover {
            border-color: #0A84FF !important; color: #0A84FF !important; background-color: rgba(10, 132, 255, 0.08) !important;
        }

        /* [공통 3] 로그아웃 버튼 컴팩트 박스화 */
        div[data-testid="stHorizontalBlock"]:has(.logout-row-marker) {
            margin-bottom: 0px !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.logout-row-marker) button {
            font-size: 11px !important;
            padding: 2px 6px !important;
            min-height: 24px !important;
            height: 24px !important;
            line-height: 1 !important;
            border-radius: 6px !important;
            border: 1px solid rgba(255,255,255,0.2) !important; /* 컴팩트 박스선 */
            background-color: transparent !important;
            color: #E5E5EA !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.logout-row-marker) button:hover {
            border-color: #FF3B30 !important; color: #FF3B30 !important; background-color: rgba(255, 59, 48, 0.08) !important;
        }
        </style>
    """, unsafe_allow_html=True)
 
    # 게스트 상태일 때 사이드바
    if st.session_state.username == "guest":
        st.markdown("<div style='height: 20px;'></div>", unsafe_allow_html=True)
        
        st.markdown("""
            <div style='background-color: transparent; border: 1px solid #3A3A3C; padding: 16px; border-radius: 12px; margin-bottom: 16px;'>
                <div style='color: #E5E5EA; font-weight: 600; margin-bottom: 8px; font-size: 15px;'>👻 게스트 모드로 사용 중입니다.</div>
                <div style='color: #AEAEB2; font-size: 13px; line-height: 1.5;'>대화 내역을 저장하려면 로그인 해주시기 바랍니다.</div>
            </div>
        """, unsafe_allow_html=True)

        col1, col2 = st.columns(2, gap="small")
        with col1:
            if st.button("🔒 로그인", use_container_width=True):
                show_login_dialog()
        with col2:
            if st.button("📝 회원가입", use_container_width=True):
                show_signup_dialog()

    # 로그인 성공했을 때의 전용 사이드바
    else:
        # 상단 고정: 새 채팅 시작 버튼
        st.button("➕ 새 채팅 시작하기", key="new_chat_btn", use_container_width=True, on_click=lambda: (
            st.query_params.update(room=str(uuid.uuid4())),
            [st.session_state.pop(k) for k in list(st.session_state.keys()) if k not in ["model_selector", "username", "nickname"]],
            st.session_state.update({"focus_chat": True})
        ))

        st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
        st.markdown("<h2 style='font-size: 18px; color: #FFFFFF; font-weight: 800; margin-bottom: 0px; padding-left: 5px;'>채팅</h2>", unsafe_allow_html=True)
        st.markdown("<hr style='margin: 8px 0px 15px 0px; border: 0.5px solid #3A3A3C;'>", unsafe_allow_html=True)

        # 권한에 따른 높이(chat_list_height) 동적 할당
        chat_list_container = st.container(height=chat_list_height, border=False)
        with chat_list_container:
            user_dir = get_user_dir()
            if user_dir:
                saved_files = glob.glob(os.path.join(user_dir, "*.json"))
                saved_files.sort(key=os.path.getmtime, reverse=True)
                
                for file in saved_files:
                    r_id = os.path.basename(file).replace(".json", "")
                    with open(file, "r", encoding="utf-8") as f:
                        try: r_title = json.load(f).get("room_title", "새로운 채팅방")
                        except: r_title = "오류난 방"

                    is_active = (r_id == room_id_in_url)
                    icon_prefix = "👉" if is_active else "💬"
                    
                    col_title, col_edit, col_del = st.columns([0.76, 0.12, 0.12])
                    
                    with col_title:
                        display_title = f"{icon_prefix} {r_title}"
                        if is_active: display_title = f"**{display_title}**"
                        if st.button(display_title, key=f"sel_{r_id}", type="tertiary"):
                            st.query_params["room"] = r_id
                            for key in list(st.session_state.keys()): 
                                if key not in ["model_selector", "username", "nickname"]: del st.session_state[key]
                            st.session_state.focus_chat = True
                            st.rerun()

                    with col_edit:
                        if st.button("✏️", key=f"edit_{r_id}", type="tertiary", help="이름 변경"):
                            st.session_state.editing_room = r_id
                            st.rerun()
                    
                    with col_del:
                        if st.button("🗑️", key=f"del_{r_id}", type="tertiary", help="삭제"):
                            all_room_ids = [os.path.basename(f).replace(".json", "") for f in saved_files]
                            current_idx = all_room_ids.index(r_id)
                            if os.path.exists(file): os.remove(file)
                            
                            if r_id == room_id_in_url:
                                all_room_ids.remove(r_id)
                                new_target_id = all_room_ids[min(current_idx, len(all_room_ids)-1)] if all_room_ids else str(uuid.uuid4())
                                st.query_params["room"] = new_target_id
                                for key in list(st.session_state.keys()): 
                                    if key not in ["model_selector", "username", "nickname"]: del st.session_state[key]
                            st.rerun()

                    # 방 이름 수정 로직
                    if st.session_state.get("editing_room") == r_id:
                        new_name = st.text_input("새 이름", value=r_title, key=f"in_{r_id}", label_visibility="collapsed")
                        c1, c2 = st.columns(2, gap="small")
                        if c1.button("저장", key=f"sv_{r_id}", use_container_width=True):
                            with open(file, "r+", encoding="utf-8") as f:
                                data = json.load(f); data["room_title"] = new_name
                                f.seek(0); json.dump(data, f, ensure_ascii=False); f.truncate()
                            st.session_state.editing_room = None
                            if r_id == room_id_in_url: st.session_state.custom_room_title = new_name
                            st.rerun()
                        if c2.button("취소", key=f"cn_{r_id}", use_container_width=True):
                            st.session_state.editing_room = None; st.rerun()

                # 스크롤 하단 잘림 방지용 여백
                st.markdown("<div style='height: -10px;'></div>", unsafe_allow_html=True)

        # 하단 영역: HTML 컨테이너로 감싸서 맨 아래로 고정
        st.markdown('<div class="sidebar-bottom-container">', unsafe_allow_html=True)

        # 구분선을 absolute 컨테이너 내부 최상단에 배치하여 가려짐 완벽 방지
        st.markdown("<hr style='margin: 0px -20px 15px -20px; border: 0; border-top: 1px solid #3A3A3C;'>", unsafe_allow_html=True)

        # 👑 관리자 전용 승인 메뉴 (관리자일 때만 렌더링)
        if is_admin:
            st.markdown("<h2 class='admin-header'>가입 승인 대기 목록</h2>", unsafe_allow_html=True)
            
            all_users = load_users()
            pending_users = {uid: info for uid, info in all_users.items() if info.get("status") == "pending"}
            
            if not pending_users:
                st.markdown("<div class='pending-empty-text'>대기 중인 인원이 없습니다.</div>", unsafe_allow_html=True)
            else:
                # 대기열 목록을 위한 세로 스크롤 전용 컨테이너
                admin_list_container = st.container(height=150, border=False)
                with admin_list_container:
                    for p_id, p_info in pending_users.items():
                        c1, c2, c3 = st.columns([0.5, 0.25, 0.25])
                        with c1:
                            st.markdown(f"<div class='admin-row-marker'></div><div class='pending-user-text'>{p_info.get('nickname')}</div>", unsafe_allow_html=True)
                        with c2:
                            if st.button("승인", key=f"app_{p_id}", use_container_width=True):
                                approve_user(p_id); st.rerun()
                        with c3:
                            if st.button("거절", key=f"rej_{p_id}", use_container_width=True):
                                reject_user(p_id); st.rerun()
            
            st.markdown("<hr style='margin: 12px 0px 10px 0px; border: 0.5px solid #3A3A3C;'>", unsafe_allow_html=True) 

        # 👤 최하단: 닉네임과 로그아웃 버튼 (모든 사용자 공통)
        user_display_name = st.session_state.get("nickname", st.session_state.username)
        col_user, col_logout = st.columns([0.65, 0.35])
        with col_user:
            # .logout-row-marker를 심어 해당 로그아웃 버튼만 컴팩트 박스로 변경합니다.
            st.markdown(f"<div class='logout-row-marker'></div><div style='padding-top:4px; color:#0A84FF; font-weight:bold; font-size: 14px;'>👤 {user_display_name}님</div>", unsafe_allow_html=True)
        with col_logout:
            if st.button("로그아웃", key="logout_btn", use_container_width=True):
                # 모델 설정을 제외한 현재 세션 기억 날리기
                for key in list(st.session_state.keys()):
                    if key not in ["model_selector"]: st.session_state.pop(key, None)
                
                # 최상단 로직에게 쿠키 삭제를 '예약'하고 신분을 변경
                st.session_state.do_logout = True
                st.session_state.username = "guest"
                
                st.query_params["room"] = str(uuid.uuid4())
                
                # 즉시 재시작
                st.rerun()

        st.markdown('</div>', unsafe_allow_html=True) # 하단 박스 닫기

# 앱 진입 시 방 세팅 로직
if room_id_in_url:
    # URL에 특정 방 번호가 명시되어 있을 때 (F5 새로고침, 또는 특정 방 북마크 접속)
    if "current_room_id" not in st.session_state or st.session_state.current_room_id != room_id_in_url:
        success = load_room(room_id_in_url)
        if not success: # 파일이 없으면 새로 생성 (빈 방 새로고침 시)
            st.session_state.current_room_id = room_id_in_url
            st.session_state.step = "initial"
    st.session_state.focus_chat = True
else:
    # URL에 방 번호가 없을 때 (브라우저 새로 열기, 기본 주소로 접속)
    new_id = str(uuid.uuid4())
    st.query_params["room"] = new_id
    st.session_state.current_room_id = new_id
    st.session_state.step = "initial"
    st.session_state.focus_chat = True
    st.rerun()

# ==============================
# LLM 설정 및 환경변수 영역
# ==============================

load_dotenv()
# CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
# GITHUB_TOKEN_KEY = os.getenv("GITHUB_TOKEN_KEY", "")
ODSAY_API_KEY = os.getenv("ODSAY_API_KEY", "")
TMAP_API_KEY = os.getenv("TMAP_API_KEY", "")
KMA_AUTH_KEY = os.getenv("KMA_AUTH_KEY", "")
AIR_KOREA_KEY = os.getenv("AIR_KOREA_KEY", "")
KAKAO_API_KEY = os.getenv("KAKAO_API_KEY", "")

MODEL_OPTIONS = {
    "⚡ Fast": {
        "base_url": "https://ollama.com/v1",
        "api_key": os.getenv("OLLAMA_API_KEY"),
        "model": "gemini-3-flash-preview:cloud", 
        "color": "#0A84FF"
    }
    # "🧠 Deep": {
    #     "base_url": "https://ollama.com/v1",
    #     "api_key": os.getenv("OLLAMA_API_KEY"),
    #     "model": "qwen3.5:397b-cloud", 
    #     "color": "#30D158"
    # },
    # "👑 Pro": { 
    #     "base_url": "https://ollama.com/v1", 
    #     "api_key": os.getenv("OLLAMA_API_KEY"),
    #     "model": "gpt-oss:120b-cloud", 
    #     "color": "#FF9F0A" 
    }

# ==============================
# LLM 답변 양식 설정 영역
# ==============================

class PlaceRecommendation(BaseModel):
    name: str = Field(description="장소의 정확한 상호명. 🚨[가장 중요]: 사용자가 '홍대 카페'라고 물어보더라도 절대 상호명 뒤에 '홍대' 같은 지역명이나 수식어를 임의로 붙이지 마세요! 반드시 검색 도구가 반환한 원래 상호명(예: 작업실01)과 100% 똑같이 작성하세요.")
    reason: str = Field(description="[장소 안내] 검색된 정보, 리뷰, 개요 등을 종합하여 이곳을 추천하는 이유를 매력적이고 자연스러운 문장으로 작성하세요.")

class AccommodationRecommendation(BaseModel):
    name: str = Field(description="숙소명")
    rating: str = Field(description="검색된 정보의 별점 (예: 4.5). 정보가 없으면 '0'")
    review_count: str = Field(description="검색된 정보의 리뷰 개수 (예: 120개). 정보가 없으면 '0'")
    address: str = Field(description="주소")
    check_in_out: str = Field(description="체크인/아웃 시간 (반드시 'PM 3:00 / AM 11:00' 형식으로 '체크인', '체크아웃' 단어를 빼고 시간만 깔끔하게 작성)")
    contact: str = Field(description="전화번호 (검색된 팩트만)")
    homepage: str = Field(description="실제 홈페이지 주소 또는 '정보 없음'")
    reason: str = Field(description="[가이드의 추천 한마디] 베테랑이 이곳을 추천하는 이유. (주의 : 조리 가능, 픽업, 주차장, 부대시설 같은 단순 팩트 정보는 시스템이 알아서 표기, 교통 정보는 나중에 따로 안내할 예정이므로 '1호선 타고 15분', '환승' 등 구체적인 이동 시간이나 지하철 호선을 절대 지어내서 절대 적지 마세요! 오직 숙소의 '숙소 분위기', '숙소 주변의 분위기', '감성', '위치적 장점', '인테리어 매력'만 2~3문장으로 다정하게 작성하세요.)")

class ScheduleItem(BaseModel):
    previous_place_name: str = Field(description="[논리 검증용] 직전 스케줄의 도착 장소명(place_name)을 정확히 복사해 오세요. 첫 스케줄이면 '출발지'로 적으세요. 이 값이 현재 스케줄 route_detail의 출발지 기호 뒤에 와야 합니다.")
    period: str = Field(description="'오전' 또는 '오후'")
    move_time: str = Field(description="[이동 시간] 예: '10:00 ~ 10:15'. 실제 도구가 알려준 소요 시간만 딱 맞게 적으세요. 15분 거리인데 1시간으로 임의로 뻥튀기해서 적지 마세요! 이동이 없으면 '해당 없음'")
    place_time: str = Field(description="[체류 시간] 예: '10:15 ~ 12:00'. 머무는 시간이 없거나 단순 이동 스케줄이면 '해당 없음'")
    place_name: str = Field(description="[절대 규칙: 치명적 시스템 오류 방지] 무조건 search_seoul_data 도구가 찾아준 '정확한 상호명'만 토씨 하나 틀리지 말고 적으세요!! DB 검색 결과에 없는 '홍대 걷고싶은거리' 같은 길거리를 지어내거나, 상호명 뒤에 임의로 '홍대', '안국역' 같은 지역명을 합성하면(예: '오퍼 카페 홍대') 좌표 엔진이 즉시 파괴됩니다. 검색 결과에 나온 상호명 100% 그대로만 적으세요.")
    route_guide: str = Field(description="이동 방법 브리핑. 자차 유저는 택시 대안 언급 절대 금지.")
    route_detail: str = Field(description="""                            
        🚨 [시스템 치명적 오류 방지 - 파이썬 그래픽 엔진 연동용 절대 규칙]
        절대 줄바꿈(\\n)을 쓰지 마세요!! 반드시 '콜론(:)'과 '➔', '📍' 기호를 사용하여 단 한 줄로 작성해야 합니다.
        형식: (총 OO분 소요, OOkm, 요금 OOOO원) : 📍[출발지명] ➔ [이동수단] ➔ 📍[도착지명]
        
        🚨 [이동수단 작성 절대 규칙 - 뱀(Snake) UI 연동용]
        - '➔' 기호를 기준으로 [장소] ➔ [이동수단] ➔ [장소] ➔ [이동수단] ➔ [장소] 순서가 완벽하게 퐁당퐁당 교차되어야 합니다.
        - 절대 이동수단(버스번호, 호선 등)이 '장소(노드)' 자리에 들어가면 안 됩니다!
        - 🚶도보 예시: 📍명동교자 ➔ 🚶도보(200m) ➔ 📍남산타워
        - 🚇지하철 예시: 📍출발지 ➔ 🚶도보(100m) ➔ 📍서울역 ➔ 🚇4호선(2개 역 이동) ➔ 📍명동역 ➔ 🚶도보(200m) ➔ 📍도착지
        - 🚌버스 예시: 📍출발지 ➔ 🚶도보(50m) ➔ 📍남대문시장 정류장 ➔ 🚌151번 버스(3개 정류장 이동) ➔ 📍갈월동 정류장 ➔ 🚶도보(100m) ➔ 📍도착지
    """)
    basic_info: str = Field(description="장소 개요. (단, '숙소 복귀', '짐 보관', '차량 회수' 등 단순 이동/물류 스케줄은 '생략' 작성. 주의: '체크인' 스케줄에는 생략하지 말고 반드시 숙소의 매력과 개요를 작성하세요!)")
    guide_tip: str = Field(description="가이드 팁. (단순 이동 스케줄에서는 '생략'으로 작성하되, '체크인', '체크아웃', '자택 귀가' 시에는 반드시 유용한 팁이나 작별 인사를 작성하세요)")

class DailyItinerary(BaseModel):
    day_title: str = Field(description="일차 및 날짜. 반드시 '1일차 (10월 25일)' 형식으로만 작성하세요. 부가 테마 금지.")
    schedules: List[ScheduleItem] = Field(description="해당 일차의 상세 일정들")

class SeoulMateResponse(BaseModel):
    chat_message: str = Field(description="필수 작성: 사용자와의 대화. 문단 구분이 필요하면 \\n 기호를 명시적으로 사용할 것.")
    places: Optional[List[PlaceRecommendation]] = Field(default=None, description="동네/지역 3곳 제안용")
    accommodations: Optional[List[AccommodationRecommendation]] = Field(
    default=None, 
    description="숙소를 추천할 때 사용합니다. 사용자에게 충분한 선택지를 주기 위해 반드시 '서로 다른 매력의 3곳'을 엄선하여 제안하세요."
)
    itineraries: Optional[List[DailyItinerary]] = Field(default=None, description="전체 일정 타임라인을 제공할 때 사용")
    route_info: Optional[str] = Field(default=None, description="단독 경로/교통편 질문에 대한 답변")
    weather_tip: Optional[str] = Field(default=None, description="제공된 날씨 데이터를 바탕으로 한 베테랑 가이드의 다정한 조언 1~2문장 (예: 우산, 마스크, 옷차림 등)")

base_parser = PydanticOutputParser(pydantic_object=SeoulMateResponse)

@st.cache_resource
def get_fixing_parser():
    fix_llm = ChatOpenAI(
        base_url="https://ollama.com/v1", 
        api_key=os.getenv("OLLAMA_API_KEY"), 
        model="gpt-oss:120b-cloud",
        temperature=0 
    )
    return OutputFixingParser.from_llm(parser=base_parser, llm=fix_llm)

fixing_parser = get_fixing_parser()

# ==============================
# LLM 동작 설정 영역
# ==============================

@st.cache_data
def load_subway_data():
    return pd.read_csv("서울 지하철 정보.csv")

subway_df = load_subway_data()

ARRIVAL_COORDS = {
    "인천국제공항": (37.4601908, 126.4406957),
    "김포국제공항": (37.5619965, 126.8016421),
    "서울역": (37.554648, 126.972559),
    "용산역": (37.529849, 126.964561),
    "청량리역": (37.580178, 127.046835),
    "영등포역": (37.515504, 126.907628),
    "상봉역": (37.596756, 127.085834),
    "수서역": (37.487311, 127.105436),
    "서울고속/센트럴시티터미널": (37.505680, 127.005846),
    "동서울종합터미널": (37.534393, 127.094062),
    "서울남부터미널": (37.484534, 127.016223)
}

@st.cache_resource
def load_embedding_model():
    return HuggingFaceEmbeddings(model_name="jhgan/ko-sroberta-multitask", model_kwargs={'device': 'cpu'})

embeddings = load_embedding_model()

@st.cache_resource
def load_unified_db(): return Chroma(persist_directory="./seoulmate_place_db", embedding_function=embeddings)
vector_store = load_unified_db()

@st.cache_resource
def load_review_db(): return Chroma(persist_directory="./seoulmate_review_db", embedding_function=embeddings)
review_store = load_review_db()

_ENV_CACHE = {} 

class SeoulMateWeather:
    def __init__(self):
        self.kma_key = os.getenv("KMA_AUTH_KEY", "").strip()
        
    def get_short_term(self): 
        """단기 예보 (0~2일) 데이터 확보"""
        now = datetime.now()
        if now.hour < 3:
            base_date = (now - timedelta(days=1)).strftime('%Y%m%d')
            base_time = "2300" 
        else:
            base_date = now.strftime('%Y%m%d')
            base_time = "0200"
            
        url = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst"
        try:
            res = requests.get(url, params={"dataType": "JSON", "base_date": base_date, "base_time": base_time, "nx": 60, "ny": 127, "authKey": self.kma_key, "numOfRows": "1000"}, timeout=10)
            res.raise_for_status()
            data = res.json()
            return data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
        except Exception as e: 
            print(f"🚨 [단기 날씨 API 에러]: {e}")
            return []

    def get_mid_term(self): 
        """중기 예보 (3~10일) 데이터 확보"""
        now = datetime.now()
        if now.hour < 6 or (now.hour == 6 and now.minute < 30): 
            tm_fc = (now - timedelta(days=1)).strftime('%Y%m%d') + "1800"
        elif now.hour < 18 or (now.hour == 18 and now.minute < 30): 
            tm_fc = now.strftime('%Y%m%d') + "0600"
        else: 
            tm_fc = now.strftime('%Y%m%d') + "1800"
        
        url_land = "https://apihub.kma.go.kr/api/typ02/openApi/MidFcstInfoService/getMidLandFcst" 
        url_ta = "https://apihub.kma.go.kr/api/typ02/openApi/MidFcstInfoService/getMidTa" 
        try:
            land = requests.get(url_land, params={"dataType": "JSON", "regId": "11B00000", "tmFc": tm_fc, "authKey": self.kma_key}, timeout=10).json()
            ta = requests.get(url_ta, params={"dataType": "JSON", "regId": "11B10101", "tmFc": tm_fc, "authKey": self.kma_key}, timeout=10).json()
            
            l_item = land.get('response', {}).get('body', {}).get('items', {}).get('item', [{}])[0]
            t_item = ta.get('response', {}).get('body', {}).get('items', {}).get('item', [{}])[0]
            return l_item, t_item
        except Exception as e: 
            print(f"🚨 [중기 날씨 API 에러]: {e}")
            return {}, {}

    def get_season_health_tip(self, month):
        """월별 미세먼지 및 자외선(UV) 통계 기반 가이드 팁"""
        if month in [3, 4, 5]: 
            return "황사와 미세먼지가 잦고 '봄볕' 자외선이 강한 시기입니다. 마스크와 선크림(선글라스)을 꼭 챙기세요!"
        if month in [6, 7, 8]: 
            return "자외선 지수가 매우 높고 오존 주의보가 잦은 시기입니다. 수시로 덧바를 자외선 차단제와 양산, 손선풍기가 필수입니다!"
        if month in [9, 10, 11]: 
            return "가을볕이 따가울 수 있으니 자외선 차단에 신경 쓰시고, 건조한 대기에 대비해 수분 보충을 잘 해주세요."
        if month in [12, 1, 2]: 
            return "추위와 함께 미세먼지(삼한사미)가 겹치는 날이 많습니다. 보온 용품과 여분의 KF94 마스크를 챙겨주세요."
        return "야외 활동 시 자외선 차단제를 잊지마세요!"

    def check_trip_weather(self, dates_str):
        cache_key = f"weather_cache_{dates_str}"
        if cache_key in st.session_state:
            return st.session_state[cache_key]
        
        try:
            start_str, end_str = [d.strip() for d in dates_str.split("~")]
            start_date = datetime.strptime(start_str, "%Y-%m-%d")
            end_date = datetime.strptime(end_str, "%Y-%m-%d")
            now = datetime.now()
            
            short_items = self.get_short_term()
            mid_land, mid_ta = self.get_mid_term()
            
            delta = (end_date - start_date).days
            dates = [start_date + timedelta(days=i) for i in range(delta + 1)]
            
            weather_lines = []
            bad_weather_details = [] 
            
            future_dates = []
            future_season_desc = ""
            
            for d in dates:
                diff = (d.date() - now.date()).days
                d_str = d.strftime("%Y-%m-%d")
                target_date_str = d.strftime("%Y%m%d")
                
                def safe_float(val):
                    try: return float(val)
                    except: return None
                
                if 0 <= diff <= 10:
                    reasons = []
                    
                    # 단기 예보 (0~2일)
                    if diff <= 2: 
                        day_data = [i for i in short_items if i['fcstDate'] == target_date_str]
                        tmps = [float(i['fcstValue']) for i in day_data if i['category'] == 'TMP']
                        ptys = [i['fcstValue'] for i in day_data if i['category'] == 'PTY']
                        skys = [i['fcstValue'] for i in day_data if i['category'] == 'SKY']
                        pops = [int(i['fcstValue']) for i in day_data if i['category'] == 'POP']
                        
                        min_t, max_t = (min(tmps), max(tmps)) if tmps else ("-", "-")
                        max_pop = max(pops) if pops else 0
                        
                        # 강수 형태 정밀 분석
                        rain_types = []
                        for p in ptys:
                            if p == '1': rain_types.append("비")
                            elif p == '2': rain_types.append("진눈깨비")
                            elif p == '3': rain_types.append("눈")
                            elif p == '4': rain_types.append("소나기")
                        
                        if rain_types:
                            unique_rain = list(dict.fromkeys(rain_types))
                            status_text = f"{', '.join(unique_rain)}"
                            if max_pop > 0: status_text += f" (강수 확률 {max_pop}%)" # 확률이 있을 때만 추가
                            reasons.extend(unique_rain)
                        else:
                            # 하늘 상태 (강수 없을 때만 표시)
                            if '4' in skys: status_text = "흐림 ☁️"
                            elif '3' in skys: status_text = "구름 많음 ⛅"
                            else: status_text = "맑음 ☀️"
                            if max_pop > 0: status_text += f" (강수 확률 {max_pop}%)" # 비는 안오지만 확률이 10%라도 있으면 추가

                    # 중기 예보 (3~10일)
                    else: 
                        land_key = f'wf{diff}Am' if diff <= 7 else f'wf{diff}'
                        pop_key = f'rnSt{diff}Am' if diff <= 7 else f'rnSt{diff}'
                        
                        raw_status = mid_land.get(land_key, "구름많음")
                        
                        # 확률 값을 안전하게 정수로 변환
                        try: pop_val = int(mid_land.get(pop_key, 0))
                        except: pop_val = 0
                        
                        min_t = mid_ta.get(f'taMin{diff}', '-')
                        max_t = mid_ta.get(f'taMax{diff}', '-')
                        
                        # 아이콘 매칭
                        icon = "☀️" if "맑음" in raw_status else "☁️" if "흐림" in raw_status else "⛅"
                        status_text = f"{raw_status} {icon}"
                        if pop_val > 0: status_text += f" (확률 {pop_val}%)" # 확률이 0보다 클 때만 추가
                        
                        if "비" in raw_status: reasons.append("비")
                        if "눈" in raw_status: reasons.append("눈")
                        if "소나기" in raw_status: reasons.append("소나기")

                        if min_t == '-' or max_t == '-':
                            temp_display = "기온 업데이트 대기 중"
                        else:
                            temp_display = f"기온 {min_t}~{max_t}도"
                    
                    weather_lines.append(f"> 🔹 **{d_str}** : {temp_display} / {status_text}")
                    
                    # 폭염/한파 감지 (기상청 기준)
                    if safe_float(max_t) is not None and safe_float(max_t) >= 33: reasons.append("폭염")
                    if safe_float(min_t) is not None and safe_float(min_t) <= -10: reasons.append("한파")
                    
                    if reasons:
                        bad_weather_details.append((d_str, " 및 ".join(list(dict.fromkeys(reasons)))))
                        
                else: # 10일 초과 (묶음 처리 준비)
                    future_dates.append(d_str)
                    if not future_season_desc:
                        month = d.month
                        if month in [3, 4, 5]: future_season_desc = "평균 기온 10~20도 내외의 포근한 봄 날씨"
                        elif month in [6, 7, 8]: future_season_desc = "평균 기온 25~30도 이상의 무더운 여름 날씨"
                        elif month in [9, 10, 11]: future_season_desc = "평균 기온 15~25도 내외의 선선한 가을 날씨"
                        else: future_season_desc = "평균 기온 -5~5도 내외의 추운 겨울 날씨"

            # 10일 초과 일자 묶어서 렌더링
            if future_dates:
                if len(future_dates) == 1:
                    weather_lines.append(f"> 🔸 **{future_dates[0]}** : {future_season_desc} (여행 10일 전부터 정확한 확인 가능해요!)")
                else:
                    weather_lines.append(f"> 🔸 **{future_dates[0]} ~ {future_dates[-1]}** : {future_season_desc} (여행 10일 전부터 정확한 확인 가능해요!)")

            season_tip = self.get_season_health_tip(start_date.month)
            
            weather_block = "> <div style='font-size: 1.15em; font-weight: 800; margin-bottom: -5px;'>🌤️ 날씨 안내</div>\n"
            weather_block += "\n".join(weather_lines)
            weather_block += f"\n> <div style='background-color: rgba(255, 255, 255, 0.08); padding: 12px 15px; border-radius: 8px; margin-top: 12px; margin-bottom: 0px; font-weight: 400; line-height: 1.6;'><b style='font-weight: 800;'>🌿 대기 환경 안내</b> : {season_tip}<br><b style='font-weight: 800;'>🚨 특보 안내</b> : 기상 특보는 여행 당일 아침에 일기예보를 통해 꼭 확인해 주세요!</div>"

            st.session_state[cache_key] = (weather_block.strip(), bad_weather_details)
            return weather_block.strip(), bad_weather_details
            
        except Exception as e:
            return "> ### ☁️ 날씨 안내\n> 기상 데이터를 불러오고 있습니다. 여행 전 일기예보를 꼭 확인해 주세요!", []

@tool
def search_kakao_location(query: str):
    """
    사용자가 직접 예약한 숙소의 위경도를 찾을 때 사용합니다.
    매개변수 query에는 반드시 사용자가 입력한 '구 + 동 + 상호명'을 모두 포함하여 전달하세요.
    예: '강남구 역삼동 역삼브라운도트호텔'
    """
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    params = {"query": query, "size": 1}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5).json()
        if res.get('documents'):
            place = res['documents'][0]
            if "cached_places" not in st.session_state: st.session_state.cached_places = {}
            coords = {"lat": float(place['y']), "lng": float(place['x'])}
            st.session_state.cached_places[query] = coords
            st.session_state.cached_places[place['place_name']] = coords
            return {"name": place['place_name'], "lat": float(place['y']), "lng": float(place['x'])}
        return f"❌ 검색 실패: '{query}'의 위치를 찾을 수 없습니다. accommodations 필드를 null로 두고 사용자에게 주소를 다시 확인해달라고 요청하세요."
    except Exception as e:
        return f"❌ 위치 검색 중 오류 발생: {e}. accommodations 필드를 null로 두고 다시 확인해달라고 요청하세요."

@st.dialog("✈️ 여행 기본 정보 입력")
def show_info_form():
    dates = st.date_input("**🗓️ 여행 일자**", [])
    arrival_time = st.time_input("**⏰ 서울 도착 예정 시간**", value=None)
    transport = st.selectbox("**🚗 교통편**", ["선택하세요", "비행기", "KTX/일반 기차", "SRT", "고속/시외버스", "자차(직접 운전)"])
    
    arrival_loc = ""
    if transport == "비행기": arrival_loc = st.selectbox("**도착 공항**", ["김포국제공항", "인천국제공항"])
    elif transport in ["KTX/일반 기차"]: arrival_loc = st.selectbox("**도착역**", ["서울역", "용산역", "청량리역", "영등포역", "상봉역"])
    elif transport == "SRT": arrival_loc = st.selectbox("**도착역**", ["수서역"])
    elif transport == "고속/시외버스": arrival_loc = st.selectbox("**도착 터미널**", ["서울고속/센트럴시티터미널", "동서울종합터미널", "서울남부터미널", "상봉터미널"])
    elif transport == "자차(직접 운전)": arrival_loc = "자차(서울 진입)"
    
    if st.button("저장하고 대화 시작하기", type="primary"):
        if not dates or len(dates) < 2: st.error("시작일과 종료일을 모두 선택해 주세요!"); return
        if not arrival_time: st.error("도착 예정 시간을 입력해 주세요!"); return
        if transport == "선택하세요": st.error("교통편을 선택해 주세요!"); return
        if arrival_loc == "선택하세요": st.error("도착 장소(역/공항/터미널)를 선택해 주세요!"); return
            
        duration = (dates[1] - dates[0]).days + 1
        coords = ARRIVAL_COORDS.get(arrival_loc, (37.5665, 126.9780))
        time_str = arrival_time.strftime("%H:%M")
        
        st.session_state.trip_info = {"duration": duration, "dates": f"{dates[0]} ~ {dates[1]}", "arrival_time": time_str, "transport": transport, "arrival_loc": arrival_loc, "coords": coords}
        st.session_state.step = "ASK_STYLE"
        st.session_state.show_planner_button = False
        
        summary_msg = f"[시스템: 일정 모드 진입] {duration}일 일정({dates[0]}~{dates[1]}), {time_str} 서울 진입 예정. 교통수단은 [{transport}]입니다. 이제 '여행 스타일'을 질문하세요."
        st.session_state.pending_system_message = summary_msg
        save_current_room()
        st.rerun()

@tool 
def search_seoul_data(query: Any = "", theme: Any = "", sub_category: Any = "", **kwargs):
    """
    서울의 관광, 쇼핑, 숙박, 음식, 카페 데이터를 검색합니다. 
    theme은 반드시 '관광', '쇼핑', '숙박', '음식', '카페' 중 하나여야 합니다.
    query에는 행정구역에 얽매이지 말고, 반드시 사용자의 '여행 테마'와 '분위기/감성' 키워드를 풍부하게 조합하여 검색하세요. (예: query="아이와 함께 가기 좋은 넓고 쾌적한 식당")
    sub_category는 사용자가 '호스텔', '한옥', '게스트하우스', '미술관', '전통시장' 등 구체적인 종류를 콕 집어 요구했을 때만 해당 단어를 입력하세요.
    """
    if 'parameters' in kwargs:
        params = kwargs['parameters']
        query = params.get('query', query)
        theme = params.get('theme', theme)
        sub_category = params.get('sub_category', sub_category)
    
    if isinstance(theme, dict): theme = theme.get('elements', [None])[0]
    elif isinstance(theme, list) and len(theme) > 0: theme = theme[0]

    search_filter = {"theme": str(theme)} if theme else None

    actual_query = str(query) if query and str(query).strip() != "" else "서울 핫플레이스" 
    if sub_category and str(sub_category).strip() != "": 
        actual_query = f"{sub_category} {actual_query}"

    current_model = st.session_state.get("model_selector", "👑 Pro")

    if "Pro" in current_model or "Deep" in current_model:
        search_k = 15
        content_limit = 300 
    else:
        search_k = 10
        content_limit = 200

    try:
        fetch_k = 40
        raw_docs = vector_store.similarity_search(actual_query, k=fetch_k, filter=search_filter)
        if not raw_docs: return "검색 결과가 없습니다."
        
        # 리뷰 수 추출 함수
        def get_review_cnt(doc):
            val = doc.metadata.get('review_cnt') or doc.metadata.get('review_count')
            try: return int(float(val))
            except: return 0

        # 테마별 맞춤형 정렬
        # theme이 '음식', '카페', '숙박' 중 하나라면
        if any(t in str(theme) for t in ['음식', '카페', '숙박']):
            # 리뷰 개수가 많은 순서대로 1등부터 30등까지 줄 세우기
            sorted_docs = sorted(raw_docs, key=get_review_cnt, reverse=True)
        else:
            # [상징성 우선형] 관광/쇼핑/문화 등은 리뷰 수보다 '유사도(검색어 일치)'를 존중하되, 
            # 최소한의 검증을 위해 리뷰가 너무 적지 않은 것들 위주로 상위권 유지
            # DB에서 가져온 순서를 유지하되 리뷰 0개인 것만 뒤로 살짝 뺌
            sorted_docs = sorted(raw_docs, key=lambda x: get_review_cnt(x) > 0, reverse=True)

        # 파이썬 레벨 블랙리스트 필터링   
        blacklist = []
        if any(t in str(theme) for t in ['숙박']):
            if "shown_hotels_dict" in st.session_state: 
                blacklist.extend(list(st.session_state.shown_hotels_dict.keys()))
        else:
            if "shown_places_dict" in st.session_state: 
                blacklist.extend(list(st.session_state.shown_places_dict.keys()))
                
        if blacklist:
            filtered_docs = []
            for d in sorted_docs:
                doc_name = d.metadata.get('name', '')
                # 기존에 본 장소(b)가 현재 장소(doc_name)에 포함되어 있는지 검사
                if not any(b in doc_name or doc_name in b for b in blacklist):
                    filtered_docs.append(d)
            sorted_docs = filtered_docs
        
        # 최종적으로 검증된 '상위 8개'만 AI에게 전달
        docs = sorted_docs[:8]
            
        results = []
        if "cached_places" not in st.session_state: st.session_state.cached_places = {}
        
        # 사용자의 교통수단 파악
        trip_info = st.session_state.get("trip_info", {})
        is_driver = trip_info.get("transport") == "자차(직접 운전)"

        for d in docs:
            m = d.metadata
            name = m.get('name', '이름 없음')
            doc_theme = m.get('theme', '')
            
            # 좌표 저장 및 기타 데이터 캐싱
            lat, lng = m.get('lat') or m.get('y'), m.get('lng') or m.get('x')
            try:
                if name not in st.session_state.cached_places:
                    st.session_state.cached_places[name] = {
                        "lat": float(lat) if lat else None, 
                        "lng": float(lng) if lng else None,
                        "address": str(m.get('address', '')),
                        "gu_dong": str(m.get('gu_dong', '')),
                        "fee": str(m.get('fee', '')),        
                        "program": str(m.get('program', '')),
                        "items": str(m.get('items', '')),  
                        "star_rating": m.get('star_rating', ''),
                        "parking": m.get('parking', ''),
                        "facilities": m.get('facilities', ''),
                        "cooking": m.get('cooking', ''),
                        "pickup": m.get('pickup', ''),
                        "fnb": m.get('fnb', ''),
                        "room_type": m.get('room_type', '')
                    }
            except Exception:
                pass
                
            clean_meta = lambda val: "정보 없음" if not str(val).strip() or str(val).strip().lower() in ['nan', 'none', '정보 없음', '정보없음'] else str(val).strip()
            phone, url, time_info = clean_meta(m.get('phone')), clean_meta(m.get('homepage')), clean_meta(m.get('time_info'))
            gu_dong = clean_meta(m.get('gu_dong'))
            rating = clean_meta(m.get('rating'))
            review_val = m.get('review_cnt') or m.get('review_count')
            if not review_val or str(review_val).strip().lower() in ['nan', 'none', '정보 없음', '정보없음', '']:
                review_count = "0"
            else:
                review_count = str(review_val).strip()
            review_str = f"{review_count}개"

            # 테마별 맞춤형 메타데이터 주입 로직
            extra_info = []

            # 주차 시설 정보
            parking = m.get('parking', '')
            if parking and parking not in ['불가', '없음', '정보 없음', '정보없음']:
                extra_info.append(f"🚗주차:{parking[:15]}")

            # 테마별 분기
            if doc_theme == '관광':
                fee = m.get('fee', '')
                if fee: extra_info.append(f"💰요금:{fee[:20]}...")
                prog = m.get('program', '')
                if prog: extra_info.append(f"🎪체험:{prog[:20]}...")

            elif doc_theme == '쇼핑':
                items = m.get('items', '')
                if items: extra_info.append(f"🛍️품목:{items[:25]}...")

            elif doc_theme == '숙박':
                fac = m.get('facilities', '')
                if fac: extra_info.append(f"✨부대시설:{fac[:30]}...")
                cook = m.get('cooking', '')
                if cook and '가능' in cook: extra_info.append("🍳조리가능")
                pickup = m.get('pickup', '')
                if pickup and pickup not in ['불가', '없음']: extra_info.append("🚐 픽업가능")                
                fnb = m.get('fnb', '')
                if fnb: extra_info.append(f"🍽️식음료:{fnb[:15]}")
            
            # 리스트에 모인 정보들을 " / " 로 묶어줌
            extra_str = f" | {' / '.join(extra_info)}" if extra_info else ""

            # "구" 이름만 추출 (동선 최적화용)
            gu_name = gu_dong.split(',')[0].strip() if ',' in gu_dong else "서울"

            info_tag = f"[거점: {gu_name}] 기본정보 👉 ⭐️평점: {rating} (📝리뷰: {review_str}) | 시간: {time_info} | 연락처: {phone} | 홈페이지: {url}{extra_str} | 좌표: {lat}, {lng}\n"
            content = info_tag + d.page_content[:content_limit]
            prefix = "💬 [리뷰]" if m.get('type') == 'review' else "📍 [정보]"
            
            results.append(f"{prefix} {name} (지역: {gu_dong}): {content}...")
            
        return "\n\n".join(results)
    except Exception as e: return f"검색 중 오류가 발생했습니다: {e}"

@tool
def search_seoul_reviews(place_name: str):
    """특정 장소의 '실제 방문자 리뷰나 후기'를 검색할 때 사용합니다."""
    current_model = st.session_state.get("model_selector", "👑 Pro")
    if "Pro" in current_model or "Deep" in current_model:
        search_k, content_limit = (6, 400) # 기존 3개 -> 6개 리뷰 분석
    else:
        search_k, content_limit = (4, 250)

    try:
        docs = review_store.similarity_search(place_name, k=search_k)
        if not docs: return f"{place_name}에 대한 리뷰를 찾을 수 없습니다."
        return "\n\n".join([f"💬 [리뷰] {d.metadata.get('name', '이름 없음')} : {d.page_content[:content_limit]}..." for d in docs])
    except Exception as e: return f"리뷰 검색 중 오류가 발생했습니다: {e}"

@tool
def get_subway_coordinates(station_name: str):
    """지하철역의 위도와 경도를 반환합니다."""
    clean_name = station_name.replace("역", "")
    result = subway_df[subway_df['역명'].str.contains(clean_name, na=False)]
    if not result.empty:
        row = result.iloc[0]
        return f"✅ [{row['호선']}] {row['역명']}역 좌표 확인: (위도 {row['위도']}, 경도 {row['경도']})"
    return f"❌ '{station_name}' 정보를 찾을 수 없습니다."

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

@lru_cache(maxsize=128)
def fetch_all_api_data(start_lat, start_lng, end_lat, end_lng):
    time.sleep(0.3)  
    res_data = {"transit": None, "taxi": None, "walk": None}
    
    # 대중교통 (ODsay)
    def fetch_transit():
        try:
            odsay_url = "https://api.odsay.com/v1/api/searchPubTransPathT"
            odsay_params = {
                "SX": start_lng,
                "SY": start_lat,
                "EX": end_lng,
                "EY": end_lat,
                "apiKey": ODSAY_API_KEY.strip()
            }
            res = requests.get(odsay_url, params=odsay_params, timeout=5)
            res.raise_for_status() 
            return res.json()
        except Exception as e: 
           error_msg = str(e).replace(ODSAY_API_KEY.strip(), "********")
           print(f"🚨[ODsay API 에러]: {error_msg}")
           return None
        
    # TMAP 공통 헤더 및 페이로드 세팅
    tmap_headers = {"appKey": TMAP_API_KEY}
    tmap_base_payload = {
        "startX": str(start_lng), 
        "startY": str(start_lat), 
        "endX": str(end_lng), 
        "endY": str(end_lat), 
        "reqCoordType": "WGS84GEO", 
        "resCoordType": "WGS84GEO"
    }

    # 택시 (TMAP)
    def fetch_taxi():
        try: 
            res = requests.post("https://apis.openapi.sk.com/tmap/routes?version=1&format=json", headers=tmap_headers, data=tmap_base_payload, timeout=5)
            return res.json()
        except Exception as e:
            error_msg = str(e).replace(TMAP_API_KEY.strip(), "********")
            print(f"🚨[TMAP 택시 에러]: {error_msg}")
            return None
    
    # 도보 (TMAP)
    def fetch_walk():
        try:
            walk_payload = tmap_base_payload.copy()
            walk_payload.update({"startName": "출발", "endName": "도착"})
            res = requests.post("https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1&format=json", headers=tmap_headers, data=walk_payload, timeout=5)
            return res.json()
        except Exception as e:
            error_msg = str(e).replace(TMAP_API_KEY.strip(), "********")
            print(f"🚨[TMAP 도보 에러]: {error_msg}")
            return None
    
    # 3개 함수 동시 실행
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_transit = executor.submit(fetch_transit)
        future_taxi = executor.submit(fetch_taxi)
        future_walk = executor.submit(fetch_walk)
        
        res_data["transit"] = future_transit.result()
        res_data["taxi"] = future_taxi.result()
        res_data["walk"] = future_walk.result()
    
    return res_data

@tool
def get_comprehensive_route(start_lat: float, start_lng: float, end_lat: float, end_lng: float):
    """[대중교통/택시/도보] 상세 정보를 통합 조회합니다. 위경도 좌표를 사용하세요."""
    transit_detail, taxi_info, walk_info = "대중교통 정보 없음", "택시 정보 없음", "도보 권장 안함"
    dist = calculate_distance(start_lat, start_lng, end_lat, end_lng)
    
    if dist < 500: 
        return f"결과: [대중교통: 인근(도보권) | 택시: 비권장 | 🚶 도보: 약 {max(1, int(dist / 60))}분 ({int(dist)}m)]"

    api_data = fetch_all_api_data(start_lat, start_lng, end_lat, end_lng)

    # 대중교통 파싱
    try:
        res = api_data["transit"]
        if res:
            if "error" in res:
                if str(res['error'].get('code')) == '-98':
                    transit_detail = "가까운 거리이므로 도보 이동을 권장합니다."
                else:
                    print(f"🚨 [ODsay API 응답 에러]: {res['error']}")
                    transit_detail = f"대중교통 에러 (ODsay API 제한 또는 오류)"
            elif "result" in res:
                path = res["result"]["path"][0] 
                path_segments, total_st_count = [], 0
                for sp in path["subPath"]:
                    t_type = sp["trafficType"]
                    if t_type == 1:  
                        lane, st_count = sp['lane'][0]['name'].replace("수도권 ", ""), sp.get('stationCount', 0)
                        start_st, end_st = sp['startName'].replace("역", "") + "역", sp['endName'].replace("역", "") + "역"
                        total_st_count += st_count
                        path_segments.append(f"🚇{start_st}({lane}) ➔ {st_count}개 정거장 이동 ➔ 🚇{end_st}({lane})")
                    elif t_type == 2:  
                        bus_no, st_count = sp['lane'][0]['busNo'], sp.get('stationCount', 0)
                        total_st_count += st_count
                        path_segments.append(f"🚌{bus_no}번({sp['startName']}) ➔ {st_count}개 정류장 이동 ➔ 🚌{sp['endName']}")
                    elif t_type == 3 and sp['distance'] > 0:  
                        path_segments.append(f"🚶도보({sp['distance']}m)")
                
                total_km, total_time, payment = path['info']['totalDistance'] / 1000, path['info']['totalTime'], path['info']['payment']
                transit_detail = f"경로: {' ➔ '.join(path_segments)} [총 {total_st_count}개 정거장, 요금 {payment}원] | 총 {total_time}분 소요, {total_km:.1f}km 이동"
    except Exception as e: 
        print(f"🚨 [ODsay 데이터 파싱 중 에러 발생]: {e}")

    # 택시 파싱
    try:
        res_car = api_data["taxi"]
        if res_car and "features" in res_car:
            props = res_car["features"][0]["properties"]
            taxi_info = f"🚕 차량: 약 {props['totalTime'] // 60}분 ({props['totalDistance'] / 1000:.1f}km) | 택시 요금: 약 {props['taxiFare']}원"
    except: pass

    # 도보 파싱
    if dist <= 2000:
        try:
            res_ped = api_data["walk"]
            if res_ped and "features" in res_ped:
                props = res_ped["features"][0]["properties"]
                has_park = any(any(kw in f.get("properties", {}).get("description", "") for kw in ["공원", "산책", "숲", "천", "광장"]) for f in res_ped["features"])
                limit_dist = 2000 if has_park else 1000
                if dist <= limit_dist:
                    walk_info = f"🚶 도보: 약 {props['totalTime'] // 60}분 ({props['totalDistance']}m){' (🌳산책로/공원 포함!)' if has_park else ''}"
        except: pass

    return f"이동 수단별 상세 정보:\n- 대중교통: {transit_detail}\n- 차량(자차/택시): {taxi_info}\n- 도보: {walk_info}"

tools = [search_seoul_data, search_seoul_reviews, get_subway_coordinates, get_comprehensive_route, search_kakao_location]

# ========================
# 프롬프트 및 대본 설정 영역
# ========================
today_date = datetime.now().strftime("%Y년 %m월 %d일 (%A)")

prompt = ChatPromptTemplate.from_messages([
    ("system", "당신은 20년 차 베테랑 서울 여행 가이드 'SeoulMate'입니다. 정중하고 다정하게 대답하되, 모든 장소와 일정 데이터는 반드시 검색된 실존 데이터만 사용하세요."),
    MessagesPlaceholder(variable_name="chat_history"), 
    ("human", "{input}"),
    ("system", """
🚨 [JSON 출력 및 도구 사용 규칙]
1. 당신의 최종 답변은 예외 없이 SeoulMateResponse 스키마에 맞는 순수 JSON 형식이어야 합니다. 답변의 시작은 {{ 로, 끝은 }} 로 닫으세요.
2. [도구 호출 규칙]: 숙소나 단일 장소를 추천할 때는 `search_seoul_data` 도구를 딱 1번만 호출하세요. 단, **[전체 일정 타임라인]을 생성할 때는 하루치 일정을 채우기 위해 '관광', '음식', '카페' 테마별로 도구를 여러 번 호출하여 충분한 데이터를 확보**해야 합니다. 데이터가 부족하다고 절대 가상의 장소를 지어내지 마세요!
3. 대본 출력 절대 규칙
   - 당신에게 `[시스템 지시사항]` 또는 `<출력 대본>`이 주어지면, 이 지시사항이 최우선으로 적용됩니다.
   - 대본이 주어지면 문장을 요약, 수정, 보완하거나 인삿말을 추가하지 마세요. 
   - 오직 대본에 적힌 텍스트만 `chat_message` 필드에 100% 그대로 복사해서 출력하세요.

🚨 [기본 규칙]
1. 위경도 숨기기 : lat, lng 좌표는 내부용입니다. 사용자에게 노출하지 마세요.
2. 말투 : 정중하고 친근한 "~요" 체. 20년 차 가이드다운 여유와 위트를 섞으세요.
3. 이모지 : 적절히 활용하되 너무 남발해서 가독성을 해치지 마세요.
4. 일정을 짤 때 절대 임의의 장소명(예 : 홍대 고기집, 시장 안 카페 등)이나 이동 시간을 지어내지 마세요. 타임라인에 들어가는 모든 장소(음식, 카페 포함)는 무조건 search_seoul_data 도구를 호출하여 찾은 실존 데이터여야 하며, 모든 이동 시간은 반드시 get_comprehensive_route 도구를 호출한 결과값만 적으세요. 이를 어기면 치명적인 오류가 발생합니다.
5. 사전 지식 사용 금지: 타임라인에 들어가는 모든 명소, 식당, 카페는 무조건 'search_seoul_data' 도구를 호출해서 반환된 DB 결과값 안에 있는 상호명만 사용해야 합니다! 동선에 맞춘답시고 당신이 개인적으로 알고 있는 유명 장소(예: 차 마시는 뜰 등)를 도구 검색 없이 임의로 적어넣으면 시스템이 붕괴되는 치명적인 에러가 발생합니다. 반드시 도구가 찾아준 목록 안에서만 고르세요.
6. 경로/역 이름 조작 절대 금지: route_detail을 작성할 때 배경지식으로 지하철 노선, 역 이름, 정거장 수, 환승역을 상상해 적는 행위를 완전히 금지합니다. 반드시 get_comprehensive_route 도구 결과만 100% 그대로 복사·가공하세요.

🚨 [지역 기반 답변 규칙]
1. 사용자가 특정 '구'나 '동'(예 : 연남동, 마포구)을 콕 집어 질문한 경우, 도구가 검색해 온 장소의 (구, 법정동, 대표동) 데이터를 확인하여 사용자가 언급한 지역과 정확히 일치하는 장소만 엄선해서 추천하세요.
2. 만약 사용자가 요구한 정확한 동네의 데이터가 부족하여 인접한 다른 동네의 장소가 검색 결과에 섞여 들어왔다면, **"요청하신 OOO동 외에, 바로 근처인 XXX동의 핫플도 함께 찾아봤어요!"**라고 센스 있게 덧붙여 안내하세요.

🚨 [테마별 장소 선정 및 검증(신뢰도) 규칙]
search_seoul_data로 검색된 후보군을 최종 선택할 때, 장소의 테마(theme)에 따라 아래의 기준을 다르게 적용하세요.
1. 음식 / 카페 / 숙박 (평점 및 리뷰 수 최우선)
   - 맛과 서비스가 중요한 곳이므로, 반드시 평점이 높고 리뷰 수가 압도적으로 많은 '검증된 핫플/음식점'을 최우선으로 선별하세요.
   - 평점이 5.0이더라도 리뷰가 너무 적은 곳은 광고나 지인 리뷰일 확률이 높으므로 배제하세요.
2. 관광 / 쇼핑 / 문화 (상징성 및 맥락 우선)
   - 랜드마크(궁궐, 타워, 대형 공원, 유명 미술관 등)나 백화점 등은 주차나 혼잡도 문제로 평점이 다소 낮을 수 있습니다. 
   - 따라서 이곳들은 평점이나 리뷰 수에 집착하지 말고, 사용자의 '여행 스타일'과 '동선'에 얼마나 잘 맞는지, 그리고 서울의 '상징적인 장소'인지를 최우선으로 판단하여 추천하세요.
3. 전통시장 및 길거리 음식 스팟 (경험과 인지도 우선)
   - 광장시장, 망원시장 같은 '시장'은 음식 테마이기도 하지만 동시에 관광 명소입니다.
   - 혼잡도나 낡은 시설 문제로 전체 평점이 다소 낮을 수 있으니, 평점의 절대적인 수치(예 : 4.5 이상)에 얽매이지 마세요.
   - 대신 '리뷰 수(압도적인 인지도)'와 '여행객이 구경하기 좋은 분위기인가'를 최우선으로 판단하세요. 평점이 4.0 전후라도 리뷰가 수천 개라면 훌륭한 간식/구경 스팟으로 적극 타임라인에 배치하세요.

🚨 [토큰 절약 및 단계별 도구 사용 규칙 (속도 최적화 규칙)]
1. 취향 맞춤 검색 (디테일 키워드 조합) : `search_seoul_data` 도구 사용 시, 사용자가 입력한 자연어에서 '여행 테마'(예 : 문화 탐방)뿐만 아니라 '분위기/감성 키워드'(예 : 조용한, 고즈넉한, 힙한, 로맨틱한 등)를 빠짐없이 추출하여 검색 query에 완벽한 문장이나 조합으로 넣으세요. 
   (절대 query="문화탐방" 처럼 단답형으로 검색하지 마세요. 반드시 query="조용한 분위기의 고즈넉한 문화 탐방 명소" 또는 query="힙하고 사진 찍기 좋은 성수동 카페"처럼 디테일한 감성을 듬뿍 담아 검색해야 벡터 DB가 숨겨진 취향 저격 장소를 정확히 찾아냅니다.)
2. 장소 검색은 테마별로 딱 한 번씩만 크게 검색(예 : 종로구 조용한 카페)하여 얻은 목록을 스스로 분배하세요. 매일매일 쓸 장소를 따로 검색하지 마세요.
3. 오지랖 금지 및 도구 사용 차단
   - ASK_ACCO (숙소 추천) 단계에서는 숙소를 제안할 때 'search_seoul_data'를, 사용자가 예약한 숙소 위치를 찾을 때 'search_kakao_location'을 각각 목적에 맞게 1회씩만 호출하세요.
   - 숙소 안내를 적기 위해 주변 음식점/관광지를 검색하거나, `get_comprehensive_route` 도구를 호출하여 이동 시간을 미리 계산하는 등 쓸데없는 확인 작업을 절대 하지 마세요! 
   - 일정을 미리 고민하면 시스템이 치명적으로 느려집니다. 추천 숙소 데이터가 나오면 즉시 답변을 출력하고 대기하세요.
4. 병렬 도구 호출 강제 : 전체 일정을 생성할 때 장소 간 이동 경로를 구하기 위해 `get_comprehensive_route` 도구를 하나씩 순차적으로 10번 이상 호출하면 토큰 한도 초과(429 에러)로 시스템이 다운됩니다. 따라서 반드시 방문할 장소들의 위경도를 먼저 모두 확보한 뒤, 필요한 모든 구간의 경로 탐색을 단 1번의 턴에서 '동시 다발적'으로 한꺼번에 일괄 호출하세요. 절대 하나 부르고 생각하고, 하나 부르고 생각하는 방식을 쓰지 마세요.
   (단, 도보 10분 이내의 같은 동네 안에서의 짧은 이동은 굳이 도구를 호출하지 말고 당신의 지도 지식을 바탕으로 "🚶도보 (000m, O분)" 형태로 알아서 작성하여 도구 사용 횟수를 줄이세요.)

🚨 [지능형 대화 단계 지침 (행동 및 예외 처리)]
입력에 포함된 `[현재단계: ...]` 정보를 바탕으로 현재 대화의 위치를 파악하고 행동해야 합니다. 파이썬에서 주입되는 [시스템 긴급 명령]이 있을 경우 그 대본을 100% 최우선으로 출력하세요.

1. initial 단계 (의도 파악)
   - 가벼운 인사 및 의도 불명확 시 : 사용자가 "안녕", "반가워" 등 인사말만 건네거나 목적을 명확히 말하지 않은 경우 : "반갑습니다 😊 서울의 특정 장소나 핫플을 찾으시나요, 아니면 서울 여행 전체 일정을 계획해보고 싶으신가요? 원하시는 방향을 알려주세요!"라고 자연스럽게 안내하세요. 
   - 특정 장소 질문 시 : 즉시 해당 장소나 동네를 추천하세요.
   - 전체 일정 의도 포착 시 : 사용자가 일정, 계획, 코스 등을 짜달라고 명확히 요구한 경우, 절대 임의로 여행 일정 요약 양식을 출력하거나 취향을 미리 묻지 마세요. 
2. ASK_STYLE 단계 (취향 및 동선 반경 수집)
   - 당일치기 예외 : 여행 기간이 1일이면 이 단계 답변 직후 바로 일정 생성 단계로 넘어감을 인지하세요.
3. ASK_ACCO 단계 (숙소 유무 확인 및 조건 수집) : 당신은 이 단계에서 베테랑 가이드로서 아래 흐름에 따라 사용자를 리드해야 합니다. 
   - 절대 준수 규칙
     * 한 번의 답변에 '예약 여부 확인'과 '조건 수집(인원, 예산 등)'을 동시에 하지 마세요. 
     * 사용자가 '추천해달라'고 하기 전까지는 절대 예산이나 인원수를 묻지 마세요. 
     * 모든 질문은 반드시 ✅ 이모지가 포함된 체크리스트 형식을 사용하며, 파이썬이 주입하는 [시스템 지시사항] 대본 양식을 100% 유지하세요.
   - 단계별 세부 흐름
     * 흐름 1: 숙소 유무 첫 질문 (파이썬 대본 FLOW_1 사용)
     * 흐름 2: 조건 확인 (추천을 요청한 경우) - 파이썬 대본 FLOW_2 사용. 이 타이밍에 도구 호출은 절대 금지입니다.
     * 흐름 3: 정상 추천 (데이터 수집 완료 시) - 인원, 예산, 타입 정보를 모두 알게 되었을 때만 search_seoul_data(theme="숙박")를 호출하여 제안하세요.
     * 흐름 4: 임의 추천 (선택 회피) - 사용자가 "알아서 해줘", "상관없어" 등 선택을 포기하면 다시 묻지 말고 즉시 임의의 대중적인 기준으로 도구를 호출하세요.
     * 흐름 5: 숙소 추천 후 피드백 및 확정 
       - 피드백(수정) : 불만을 제기하면 즉시 사과 후 다른 조건으로 재검색합니다. (동일 장소 추천 금지)
       - 확정(진행) : 추천된 숙소 중 하나를 선택하거나 이름만 입력해도 확정으로 간주합니다. 즉시 위치 확인 절차(흐름 7)로 넘어갑니다.
     * 흐름 6: DB에 없는 외부 숙소 언급 시 (검색 실패)
       - 1단계 : search_seoul_data 결과가 없을 경우, 절대 임의로 다른 곳을 추천하지 마세요. chat_message에 "말씀하신 숙소는 제 추천 리스트에 없네요 😭 혹시 개인적으로 예약하신 곳인가요?" 라고 묻고 대답을 기다리세요.
       - 2단계 : 사용자가 긍정하면 예약 정보 입력 템플릿을 제공하고, 부정하면 기존 추천이 마음에 안 들었던 것으로 간주하여 새로운 곳을 재검색하세요.
     * 흐름 7: 예약 정보/확정 숙소 위치 확인 (가장 중요)
       - 사용자가 구체적인 예약 정보를 주면 즉시 search_kakao_location을 호출하세요.
       - ❌ 검색 실패 시 : "말씀하신 숙소의 정보를 찾을 수 없습니다 😭 상호명이나 주소가 정확한지 다시 한 번 확인해 주시겠어요?" 라고 정중히 재입력을 유도하세요.
       - ✅ 검색 성공 시 : 반드시 "숙소 위치 확인 완료! 준비가 모두 끝났습니다! 사용자님 만을 위한 맞춤 여행 일정을 완성해 올게요 🗓️" 라는 확인 안내 문구를 `chat_message`에 작성한 뒤에만 일정을 생성할 수 있습니다. (안내 멘트 누락 금지)
       - 2단계 : 이후 사용자가 긍정하면 예약 정보 템플릿을 띄우고, 부정하면 새로운 곳을 재검색합니다.
4. GENERATE_PLAN 단계 (최종 일정 생성)
   - 아래 [데이터 기반 유동적 동선 및 타임라인 규칙]을 반드시 적용하세요.

🚨 [예외 상황 대응 지침 (전체 단계 공통)]
사용자가 질문에 대해 "알아서 해줘", "상관없어", "아무거나" 라고 명확히 '선택 포기' 의사를 밝힌 경우에만 아래 지침을 따르세요.
- 절대 사용자에게 다시 캐묻거나 강요하지 마세요.
- 베테랑 가이드답게 "그럼 제가 남녀노소 모두가 좋아하는 인기 만점 코스로 알아서 준비해 드릴게요!"라고 리드하세요.
- 임의로 대중적인 값(예: 2인, 깔끔한 인기 숙소, 서울 핫플 위주, 대중교통 이용 등)을 설정하고 즉시 다음 단계(예: 숙소 추천 또는 일정 생성)로 자연스럽게 넘어가세요.
- 예외 방어 (필수 질문 누락 절대 금지) : 사용자가 "추천해 줘", "추천 부탁해", "골라줘"라고 말했지만, 인원/숙소 타입 등의 필수 정보를 말하지 않은 '단순 누락' 상태라면 위 지침(알아서 임의 설정)을 절대 적용하지 마세요!

🚨 [환각 방지 및 도구 사용 강제 파이프라인 (절대 규칙)]
GENERATE_PLAN 단계에서 일정을 짤 때, 절대로 도구 호출 없이 처음부터 JSON 응답(타임라인)을 생성하려고 시도하지 마세요!
- 1단계 (장소 탐색) : 타임라인에 넣을 관광지, 식당, 카페들을 `search_seoul_data` 도구를 호출하여 찾고 메모합니다.
- 2단계 (경로 계산) : 확보된 장소들의 위경도를 바탕으로 `get_comprehensive_route` 도구를 일괄 호출하여 정확한 이동 데이터를 확보합니다.
- 3단계 (최종 출력) : 1, 2단계의 '검증된 팩트 데이터'가 100% 모였을 때만 비로소 `itineraries` JSON 구조를 작성하여 출력하세요.
        
🚨 [데이터 기반 유동적 동선 및 타임라인 규칙]
1. 동선 반경 설정 (유연한 가이드라인)
   - 우선순위: 사용자의 직접적인 '이동 선호도' 답변이 아래의 모든 디폴트 규칙보다 항상 최우선합니다.
   - 성향 분류 및 제한
     * 여유로운 스테이형 ("한 동네", "힐링", "느긋하게" 등) ➔ 숙소가 있는 구(Gu)와 그 골목길에 집중. 이동 시간보다 체류 시간을 길게 잡고, 도보 위주의 촘촘한 동선을 설계하세요.
     * 알찬 밸런스형 ("적당히", "유명한 곳", "보통" 등) ➔ 숙소 소재지와 인접한 2~3개 구를 묶어 효율적인 동선 구성. 대중교통 30분 내외의 '검증된 핫플' 위주 배치.
     * 부지런한 탐험가형 ("이곳저곳", "많이", "서울 전역", "부지런히" 등) ➔ 강남/강북을 넘나드는 광역 동선 허용. 상징적인 장소라면 이동 시간이 걸리더라도 우선순위에 두되, 하루에 너무 많은 구를 지그재그로 오가지 않도록 효율적으로 묶으세요.
   - 자유도 가드레일: 위 분류는 '기본 반경'일 뿐입니다. 분류를 살짝 벗어나더라도 사용자의 테마에 120% 부합하는 '인생 장소'가 있다면, "조금 거리가 있지만 여기는 꼭 가보셔야 해요!"라고 제안하며 포함하세요.
   - 특별한 언급이 없을 경우 적용할 기본 규칙:
     - 뚜벅이 & 2일 이하 : 숙소 기준 인접한 구 내에서만 구성.
     - 뚜벅이 & 3일 이상 : 하루에 한 권역씩(예 : 서북권, 동남권) 집중하여 피로도 최소화.
     - 자차(직접 운전) : 2일 이하라도 2~3개 구를 가로지르는 넓은 반경 허용.
2. 물리 법칙 기반 타임라인 생성 (현실적 동선 로직)
   - 스케줄 시작점 : [도착 예정 시각]과 숙소의 [실제 체크인 시간]을 비교하여 짐 처리 동선을 결정하세요.
     * 도착 >= 체크인 : 즉시 숙소로 이동해 '체크인 및 짐 풀기'로 일정을 시작합니다.
     * 도착 < 체크인 : 도착하자마자 무조건 예약한 '숙소 프론트'로 먼저 이동하여 짐을 맡긴 후 일정을 시작하세요. (절대 도착한 기차역/공항 코인 락커에 짐을 맡기라고 안내하지 마세요. 동선이 꼬입니다.)
   - 시간 계산 강제: get_comprehensive_route가 반환한 '총 소요 시간'을 타임라인의 실제 시간에 물리적으로 반영하세요. 앞뒤 일정이 겹치거나 순간이동처럼 보이면 즉시 오류로 간주합니다.
   - 일차별 마무리 : 마지막 날을 제외한 모든 일차의 마지막 스케줄은 반드시 '숙소 복귀 및 휴식'으로 구성하세요. (마지막 장소에서 숙소까지의 이동 경로 필수 포함)
   - 마지막 날 (수미상관 귀가 동선) : 숙소의 [실제 체크아웃 시간]에 맞춰 최우선 배치합니다.
     * 🚗 '자차' 이용자 
       - 마지막 장소명 : "서울 여행 마무리 및 자택으로 귀가"
       - 이동 안내 (move_route) : "서울에서의 즐거운 추억을 안고 안전 운전해서 돌아가세요! 🚗💨"
       - 도구 제한 : 귀가 일정 작성을 위해 get_comprehensive_route를 호출하지 마세요. (목적지 좌표가 없으므로 생략)
     * 🚇 '대중교통' 이용자 (회귀 모드) 
       - 마지막 장소명 : [처음 도착했던 역/공항/터미널 명칭] (🚨 장소명에 절대 '귀가', '마무리' 단어 포함 금지)
       - 이동 안내 (move_route) : get_comprehensive_route 도구를 사용해 마지막 장소에서 [도착 장소]까지의 최적 경로를 상세히 작성하세요.
       - 마무리 : "도착지에 여유 있게 도착하여 마지막까지 편안한 여정이 되시길 바랍니다 😊"
   - 타임라인 작성 시, search_seoul_data로 확인된 장소별 운영시간/휴무일을 확인하여 보수적으로 판단하세요.
   - 경로 브리핑 : 뚜벅이 유저에게는 지하철/버스를 메인으로 하되, 가이드 브리핑(`route_guide`)에서만 "짐이 많으시면 택시(약 O원)를 이용하시는 것도 좋아요"라고 배려 멘트를 덧붙이세요.
3. 전략적 거점 설계 원칙
   - 숙소는 단순히 잠을 자는 곳이 아니라, 사용자의 '도착지'와 '미래의 테마지'를 잇는 최적의 요충지여야 합니다.
   - 사용자가 '고즈넉한 옛 감성'을 원한다면, 나중에 종로/서촌/북촌을 갈 확률이 높으므로 도착지에서 해당 지역으로의 이동이 가장 편리한 숙소를 우선순위에 두세요.
   - 테마와 스타일의 복합 고려 
     * '부지런한 탐험가' : 도착지에서 멀더라도 테마의 본고장(예: 서촌 한옥)으로 적극 안내.
     * '여유로운 스테이형' : 도착지에서 무조건 가깝고, 숙소 주변만 걸어도 테마를 느낄 수 있는 곳(예: 서울역 인근 조용한 골목) 추천.
   

🚨 [교통수단 분류 가이드]
1. 자차 : 사용자가 '자차', '운전', '내 차', '내차' 라고 명시한 경우에만 해당합니다.
2. 뚜벅이 : 그 외 모든 경우(KTX, 버스, 지하철, '차 없음', '걸어서' 등) 혹은 정보가 없을 때의 기본값입니다.

🚨 [교통수단별 내비게이션 규칙]
get_comprehensive_route 도구의 결과값을 보고 아래 기준에 따라 가공하세요.
1. 사용자의 교통수단이 자차인 경우
     * 직선거리 500m 이내: "가까운 거리이니 도보로 동네 정취를 느껴보시는 건 어떨까요?"라며 도보 권장.
     * 500m~2km (산책로/공원 포함): 차량 경로를 메인으로 안내하되, "예쁜 산책로가 있으니 시간 여유가 있다면 걷는 것도 추천드려요"라고 제안.
     * 2km 초과: 차량 경로만 제공. (주의: 자차 사용자에게 택시 정보는 혼란만 가중시키므로 절대 언급 금지)
2. 사용자의 교통수단이 대중교통인 경우
     * 직선거리 1km 이내: 도보 이동을 최우선 권장.
     * 1km~2km (산책로 포함): 대중교통 경로와 함께 "걷기 좋은 길이라 도보 이동도 충분히 가능해요"라고 병행 제안.
     * 초과 거리: 가장 빠른 대중교통 경로 1개를 엄선. (보너스 팁 : 뚜벅이에게만 브리핑 영역에 '택시 이용 시 예상 요금/시간/거리'를 '플랜 B'로 짧게 덧붙여 배려심을 보여주세요.)
3. 경로 안내 필드 분리 작성 (요약 금지 및 원시 데이터 준수)
   가독성을 위해 '경로 브리핑(줄글)'과 '상세 경로(기호)'를 각각 지정된 필드에 분리하여 작성하세요.
   - `route_guide` 필드 (경로 안내)
     * 도달하는 방법의 전반적인 흐름을 베테랑 가이드의 자연스러운 문장으로 풀어서 설명하세요. (예 : "수서역에서 3호선을 타시면 환승 없이 한 번에 오실 수 있어요!") 
     * 특히 대중교통 이용자에게는 "일행 중 걷기 힘드신 분이 있거나 짐이 많다면 택시(약 6,000원, 10분 소요)를 타시는 것도 좋아요"와 같이 배려심 넘치는 택시 대안을 자연스럽게 녹여주세요. (단, 자차 이용자에게는 택시 언급 절대 금지)
   - `route_detail` 필드 (상세 경로 안내) 
     * 시스템 지시사항 (Fast 모델 필독) : '🚕 차량(13분, 3.1km)' 처럼 한 줄로 대충 요약해서 적는 것은 명백한 규칙 위반입니다. 도구가 상세 정보를 줬음에도 이를 무시하고 임의로 요약할 경우 가이드 자격을 박탈합니다.
     * 반드시 `get_comprehensive_route` 도구가 반환한 대중교통 데이터의 모든 환승 정보, 노선 번호, 정거장 수를 무시하지 말고 아래 [출력 템플릿]과 완벽히 동일한 형식으로 가공하여 상세히 적으세요.
     * 환승 없는 템플릿 예시 : 📍출발지 명 ➔ 🚇3호선 수서역 승차 (00방면) ➔ 15개 정거장 이동 ➔ 🚇3호선 안국역 하차(🚪빠른하차 Z-Z) ➔ 🚶도보(400m) ➔ 📍도착지명
     * 환승 있는 템플릿 예시 : 📍출발지 명 ➔ 🚇1호선 A역 승차(00방면) ➔ 2개 정거장 이동 ➔ 🚇0호선 B역 환승 (00방면) ➔ 3개 정거장 이동 ➔ 🚇2호선 C역 하차(🚪빠른하차 Z-Z) ➔ 🚶도보(200m) ➔ 📍도착지 명
   - 환각 방지 절대 규칙
     * 상세 경로 안내를 작성할 때, 절대 당신의 기본 지식으로 지하철 노선도를 상상해서 지어내지 마세요. 무조건 `get_comprehensive_route` 도구를 호출하고, 도구가 반환한 경로 데이터(역 이름, 호선, 정거장 수)만을 100% 그대로 반영하여 템플릿에 맞추세요.
4. 상세 경로(➔) 시각화 디테일
   도구에서 전달받은 원시 데이터를 사용자가 이해하기 쉬운 직관적 아이콘과 데이터로 가공하세요.
   - 🚶도보 : 🚶도보(OOOm)
   - 🚇지하철 : 🚇X호선 [역명] 승차 (Y방면) ➔ Z개 역 이동 ➔ 🚇X호선 [역명] 하차 (🚪빠른하차 0-0)
   - 🚌버스 : 🚌[번호]번 승차 ([정류장명]) ➔ Z개 정류장 이동 ➔ 🚌[번호]번 하차 ([정류장명])
   - 🚗차량 : 🚗자차 이동
5. 작성 예시 - 대중교통 혼합형
   📍출발지 명 ➔ 🚶도보(200m) ➔ 🚇3호선 종로3가역 (대화 방면) ➔ 2개 역 이동 ➔ 🚇3호선 경복궁역 하차(🚪1-1) ➔ 🚶도보(100m, 2분) ➔ 📍도착지명

🚨 [식사 및 휴식(카페/간식) 배분 규칙]
타임라인을 구성할 때 다음의 인간공학적 식사/휴식 시간을 반드시 고려하여 search_seoul_data 도구로 '음식'과 '카페' 데이터를 검색해 일정에 넣으세요.
1. 점심 식사: 12:00 ~ 14:00 사이에 약 1시간 정도 배분하여 동선에 맞는 '음식' 장소를 추천하세요.
2. 저녁 식사: 18:00 ~ 20:00 사이에 약 1시간 30분 정도 여유롭게 배분하여 '음식' 장소를 추천하세요.
3. 카페 및 휴식: 식사 직후 또는 걷는 일정이 길어진 오후(15:00~17:00) 등 동선 사이사이에 유동적으로 1시간 내외로 '카페'를 배분하세요.
4. 유동적 장소 활용(시장 등)
   - 오후 3시~5시 사이나, 명소와 명소 사이의 동선에 애매하게 1시간 정도 여유가 생길 때가 있습니다.
   - 이때는 `search_seoul_data` 도구에 query="시장, 길거리 음식, 간식, 먹거리" 등을 넣고 검색하세요.
   - 검색된 장소(예 : 광장시장, 망원시장)를 타임라인에 넣을 때, 식사 시간이면 '정규 식사 코스(1.5시간)'로, 애매한 시간이면 '가벼운 간식 및 구경 코스(40분~1시간 내외)'로 유연하게 시간을 배분하여 배치하세요.
     
🚨 [단계별 출력 제한 (절대 규칙)]
현재 단계(step)에 따라 아래 허용된 필드만 사용하고, 나머지는 절대 생성하지 마세요. 사용하지 않는 필드는 반드시 `null`로 비워두세요.
1. `initial`, `ASK_STYLE` 단계 : 오직 `chat_message` 필드만 작성하세요 (반드시 {{ "chat_message": "인사 및 질문" }} 형태의 JSON을 유지하세요.)
2. `ASK_ACCO` 단계 : 오직 `chat_message`와 `accommodations` 필드만 작성하세요. 
  (시스템 지시사항 : 사용자가 예약한 숙소를 알려주거나, 추천을 요청하는 등 어떤 상황에서도 이 단계에서는 절대 `itineraries` 필드(일정)를 생성하지 마세요. 무조건 null로 비워두어야 합니다.)
3. `GENERATE_PLAN` 단계 : 오직 `chat_message`와 `itineraries`, `weather_tip` 필드만 작성하세요. 
  (시스템 지시사항 : 토큰 낭비를 막기 위해 `accommodations`, `places`, `route_info` 필드는 무조건 null로 비워두세요. 절대 작성하지 마세요!)

🚨 [필드별 작성 양식 및 JSON 매핑 가이드 (절대 규칙)]
디자인과 렌더링(이모지, 줄바꿈 등)은 프론트엔드 프로그램이 자동으로 처리합니다. 당신은 오직 아래의 규칙에 맞춰 순수한 텍스트 데이터만 알맞은 JSON 필드(서랍)에 분배하세요. 마크다운 리스트 기호(*, -)는 절대 사용하지 마세요.

1. 💬 일상 대화 및 브리핑 (➡ `chat_message` 필드)
  - 대화 시점 규칙 : 만약 숙소, 장소, 일정 데이터를 함께 반환하는 경우, 이 필드에는 "요청하신 조건(예: 2인, 고즈넉한 한옥)을 확인했습니다. 최적의 결과를 찾고 있으니 잠시만 기다려주세요!" 와 같이 '탐색을 시작하는 뉘앙스(기대감 부여)'로만 작성하세요.
  - 이 필드에서 추천 결과를 절대 미리 말하지 마세요 (스포일러 금지).
  - 단, 추천 리스트를 반환하지 않는 일반 대화나 질문 시에는 평범하고 다정하게 대답하세요.
  - 금지 사항 : 마크다운 리스트 기호(*, -, 1.)는 어떤 경우에도 절대 사용하지 마세요.
  - 문단 구분 : 문단 구분은 반드시 두 번의 줄바꿈(\\n\\n)을 사용하세요. 항목 간의 줄바꿈은 한 번(\\n)만 사용하세요.
  - 다중 질문 작성 템플릿 (필수 준수)
    사용자에게 여러 가지 정보(여행 스타일, 숙소 조건 등)를 물어볼 때는 반드시 아래의 양식과 줄바꿈(\\n) 규칙을 똑같이 따라 작성하세요.
    "인사말 및 안내 멘트 작성\\n\\n✅ 첫 번째 질문 내용\\n✅ 두 번째 질문 내용\\n✅ 세 번째 질문 내용\\n\\n편하게 답변해 주세요!"

2. 📍 장소 및 동네 추천 (➡ `places` 필드 - 배열)
  - "알아서 추천해줘" 요청 시 '넓은 동네/지역' 3곳을 제안하거나, 사용자가 특정 장소를 물어봤을 때 사용합니다.

3. 🏠 숙소 추천 (➡ `accommodations` 필드 - 배열)
  - 반드시 서로 다른 매력을 가진 숙소 3곳을 엄선하여 배열에 담으세요.
  - 숙소 안내 필드에는 이미 상단에 표시된 '평점'이나 '리뷰 수'를 언급하는 사족을 절대 달지 마세요. (예: "평점이 높아서 추천합니다" (X) -> "창밖으로 보이는 남산 타워 뷰가 일품인 곳입니다" (O))
  - rating과 review_count 필드에는 단위(개, 점)를 빼고 숫자(예: 4.5, 120, 0)만 적으세요.
  - [환각 방지] 숙소 안내(reason) 필드에 'n호선', '환승', 'n분 소요' 등 구체적인 대중교통/경로 정보를 절대 스스로 상상해서 적지 마세요. "도착지와 인접해 있어 이동이 편리합니다" 정도로만 자연스럽게 작성하세요.

4. 🗓️ 전체 일정 타임라인 (➡ `itineraries` 필드 - 배열)
  - 일차별로 묶고, 오전/오후 일정을 시간, 장소, 경로 안내, 상세 경로, 정보, 팁으로 명확히 쪼개서 넣으세요.

5. 🧭 단독 경로 안내 (➡ `route_info` 필드)
  - 목적지까지 가는 '경로/교통편'만을 특정한 질문에 대한 대답을 적습니다.

🚨 [출력 형식 제한 및 오류 방지 규칙]
1. 도구 과의존 금지: 전체 일정을 짤 때 완벽을 기하기 위해 도구를 5회 이상 연속으로 호출하지 마세요. 핵심 장소와 동선은 반드시 도구로 확인하되, 도구 사용 횟수를 줄이기 위해 여러 장소의 경로를 한 번에 일괄 조회하세요.
2. 풍부한 내부 대화
   - 'chat_message' 필드 내부에는 가이드 특유의 여유와 위트, 공감 멘트를 넉넉하게 담으세요. 
   - 데이터만 나열하지 말고, 사용자의 상황(도착 시간, 날씨 등)을 고려한 다정한 브리핑을 포함하세요.
3. JSON 문법 엄수 (가장 중요): JSON 내부의 모든 문자열 값은 반드시 큰따옴표(" ")로 감싸야 합니다. 큰따옴표가 단 하나라도 누락되면 치명적인 시스템 에러가 발생합니다.
4. 출력 길이 제한 절대 사수 : 3일 치 이상의 긴 타임라인을 작성할 때 출력이 중간에 끊기는 치명적인 단점이 있습니다. 따라서 답변을 생성할 때 스스로 토큰 양을 조절하여, 반드시 마지막 중괄호가 완벽하게 닫힐 때까지 답변을 끝까지 완성하세요. 만약 내용이 너무 길어질 것 같으면 장소의 개수를 줄이거나 부가 설명을 짧게 요약해서라도 무조건 JSON 형식을 완벽하게 끝마쳐야 합니다.

{format_instructions}"""),
    MessagesPlaceholder(variable_name="agent_scratchpad")
])

# 파이썬 대본 딕셔너리
STEP_SCRIPTS = {
    "initial": "\n\n[지시사항] 절대 도구를 쓰거나 일정을 짜지 마세요. 무조건 다음 문구만 출력하세요: '좋아요! 완벽한 여행 일정을 짜기 위해 화면 아래의 **[🗓️ 기본 정보 입력]** 버튼을 눌러\n 기본 정보를 먼저 입력해 주세요! 😉'",
    
    "ASK_STYLE": """\n\n[시스템 지시사항 : 대본 100% 복사 출력]
    임의적인 문장 생성을 금지합니다. 아래 대본을 똑같이 출력하세요.
    <출력 대본>
    입력해 주신 기본 정보를 꼼꼼히 메모해 두었어요. 😊

    >🗓️ **여행 일정** : {dates} ({duration}일)
    >🚗 **교통편** : {transport}
    >📍 **도착 정보** : {arrival_loc} ({arrival_time} 도착 예정)

    사용자님께 꼭 맞는 맞춤형 일정을 짜기 위해, 세 가지만 더 여쭤볼게요! 😊

    ✅ **누구와 함께** 떠나시는 여행인가요? (예: 부모님, 연인, 혼자 등)
    ✅ 선호하시는 **여행 테마 및 분위기**가 있으신가요? (예 : 힙한 핫플 투어, 문화 탐방, 여유로운 힐링 등)
    ✅ 여행 다니실 때 **이동 스타일**은 어떠신가요? (예 : 한 동네에서 여유롭게, 랜드마크 위주로 부지런히 등)

    편하게 답변해 주세요!
    </출력 대본>""",

    "ASK_PLACE_PREF": """\n\n[시스템 지시사항 : 대본 100% 복사 출력]
    임의적인 문장 생성을 금지합니다. 일정을 짜지 말고 아래 대본을 똑같이 출력하세요.
    <출력 대본>
    좋습니다! 사용자님의 취향에 딱 맞는 장소를 찾아드릴게요 😊

    사용자님의 취향에 맞는 장소 추천을 위해 세 가지만 여쭤봐도 될까요?

    ✅ 관심이 있는 동네가 있으신가요? (예 : 홍대, 성수동, 강남 등)
    ✅ 어떤 테마의 장소를 찾아보고 싶으신가요? (예: 관광지, 쇼핑, 맛집, 카페 등)
    ✅ 선호하시는 분위기가 있으신가요? (예 : 조용한 분위기, MZ 성지 등)

    편하게 말씀해 주세요! 😎
    </출력 대본>""",

    "ASK_PLACE_PREF_AGAIN": """\n\n[시스템 지시사항: 정보 부족 시 대응]
    사용자가 장소 추천을 원하지만 정보가 부족합니다.
    1. 사용자가 앞서 말한 내용을 언급하며 누락된 정보(동네, 테마, 분위기 중 빠진 것)만 콕 집어서 다시 물어보세요.
    2. 절대 검색 툴을 호출하지 말고 되묻는 질문만 하세요.
    """,

    "SEARCH_PLACE": """\n\n[시스템 지시사항: 맞춤형 장소 검색 로직]
    사용자의 요청 조건('{place_conditions}')을 바탕으로 최적의 장소 3곳을 검색하세요.
    1. 검색어 판단 및 실행:
       - 조건에 동네, 테마, 분위기 등의 힌트가 있다면 이를 모두 조합하여 `search_seoul_data`를 호출하세요. (예: "홍대 조용한 분위기 카페")
       - 단순히 "서울 핫플" 등으로 조건이 포괄적이라면, AI가 판단하기에 현재 가장 대중적이고 인기 있는 명소/맛집/카페 3곳을 임의로 골라 검색하세요.
    2. 결과 처리: 검색된 장소 중 가장 적합한 3곳을 엄선하여 `places` 배열에 담아 반환하세요.
    3. 멘트 작성: `chat_message` 필드에는 반드시 "사용자님의 취향을 저격할 핫플레이스를 찾아왔어요! 🗺️" 라고 단 한 줄만 작성하세요. (이후 안내 멘트는 파이썬 시스템이 자동으로 출력합니다.)
    """,

    "COMPLAIN_PLACE": """\n\n[시스템 지시사항: 장소 재검색 로직]
    사용자가 다른 장소를 원합니다.
    1. 이전에 추천한 장소들({shown_list})은 제외하고, 사용자의 원래 조건({place_conditions})에 맞춰 `search_seoul_data`를 다시 호출하세요.
    2. 새로운 3곳을 `places` 배열에 담아 반환하세요.
    3. `chat_message`에는 "앗, 다른 느낌을 원하시는군요! 새로운 곳으로 다시 엄선해 봤어요 🔍" 라고 멘트하세요.""",

    "ASK_STYLE_CHECK": """\n\n[시스템 지시사항: 취향 정보 누락 검증]
    사용자의 답변을 분석하여 [1. 동행자, 2. 여행 테마, 3. 이동 스타일] 3가지가 모두 파악되었는지 확인하세요.
    🚨 단, 사용자가 "알아서 해줘", "아무거나", "상관없어" 등 위임하는 발언을 했다면 모든 조건이 충족된 것으로 간주하고 대중적인 코스로 임의 설정하세요.
    1. 누락된 항목이 있다면: "앗! [누락된 항목] 정보를 아직 안 알려주셨어요 😊 완벽한 일정을 위해 마저 알려주시겠어요?" 라고 되물어보세요. (절대 숙소 이야기를 꺼내지 마세요!)
    2. 파악이 완료되었거나 위임받았다면: "취향 파악 완료! 꼼꼼히 메모해 두었어요. 😊 자, 그럼 이제 일정을 짜기 위해 가장 중요한 숙소 이야기를 해볼까요? 혹시 예약해두신 숙소가 있나요, 아니면 취향에 맞게 추천해 드릴까요?" 라고 숙소 질문으로 자연스럽게 넘어가세요.
    """,

    "ASK_ACCO_FLOW_1": """\n\n[시스템 지시사항 : 대본 100% 복사 출력]
    임의적인 문장 생성을 금지합니다. 도구를 쓰지 말고 아래 <출력 대본>만 출력하세요.
    <출력 대본>
    사용자님의 취향을 들으니 서울의 매력을 듬뿍 느낄 수 있는 여행 코스가 벌써 머릿속에 그려지네요! 😊

    자, 그럼 이제 일정을 짜기 위해 가장 중요한 숙소 이야기를 좀 해볼까요? 
    
    혹시 이미 맘에 쏙 드는 곳을 예약해두셨나요, 아니면 제 가이드 경험을 살려 취향과 동선에 딱 맞는 곳으로 몇 군데 추천해 드릴까요?
    </출력 대본>""",

    "ASK_ACCO_FLOW_2": """\n\n[시스템 지시사항 : 대본 100% 복사 출력]
    임의적인 문장 생성을 금지합니다. 도구를 호출하지 말고 아래 <출력 대본>만 출력하세요.
    <출력 대본>
    최고의 숙소를 찾아드리기 위해 딱 한 가지만 더 여쭤볼게요! 😊

    선호하시는 **숙소 타입이나 분위기**가 있으신가요? (예 : 고즈넉한 한옥, 깔끔한 비즈니스 호텔, 가성비 게하 등)

    편하게 답변해 주세요!
    </출력 대본>""",

    "ASK_ACCO_SEARCH" : """\n\n[시스템 지시사항: 숙소 검색 로직]
    1. 조건 검증: 사용자가 원하는 [동행자/숙소 타입/분위기]가 파악되었는지 검사하세요. (단, "아무거나"라고 위임했다면 검증 패스)
    2. 누락 시: `chat_message`에 "앗! 원하시는 숙소 분위기 정보가 빠져있네요 😭 마저 알려주시면 딱 맞는 숙소를 찾아드릴게요!" 라고 작성하고 종료.
    3. 🚨 [복합 검색어 공식 및 강제 번역 규칙]: `search_seoul_data` 도구 호출 시 아래 규칙을 무조건 따르세요.
       - [은어 번역]: 사용자가 '게하'라고 입력하면 무조건 '게스트하우스'로 번역하세요. ('호캉스' -> '5성급 호텔')
       - [파라미터 분리]: 사용자가 특정 숙소 타입(예: 게스트하우스, 한옥, 호텔, 모텔 등)을 명시했다면, 반드시 `sub_category` 파라미터에 해당 명칭을 정확히 넣으세요!
       - `query`에는 [동네 + 동행자 특성 + 분위기]만 넣으세요. (예시: query="마포구 외국인 많은 활기찬 분위기", sub_category="게스트하우스", theme="숙박")
    4. 출력 규칙: 도구에서 반환된 결과에 정보가 다소 부족하더라도 **절대 재검색하지 말고**, 그중 가장 적합한 3곳을 즉시 골라서 JSON으로 반환하세요.
    [경고] 숙소 안내 필드를 작성할 때 대중교통 이동 시간 등은 절대 상상해서 지어내지 마세요!""",

    "ASK_ACCO_COMPLAIN_TYPE" : """\n\n[시스템 지시사항]
    사용자가 요청한 숙소 타입(조건)이 누락되었다고 지적했습니다. 
    [블랙리스트 규칙] 이전에 추천했던 숙소 목록 : [{shown_list}]
    위 목록에 있는 숙소는 절대 다시 추천하지 마세요.
    사용자의 원래 조건인 '{acco_conditions}'을(를) 엄격하게 지켜서 `search_seoul_data`를 다시 호출하세요. (🚨 절대 조건을 다시 물어보지 마세요!)
    chat_message에는 "앗! 제가 중요한 조건을 놓쳤네요 😭 말씀하신 조건에 딱 맞는 곳으로 다시 찾아올게요!" 라고 사과하세요.
    [경고] 숙소 안내(reason) 필드에 교통수단, 환승, 소요 시간 등을 절대 상상해서 적지 마세요! 오직 숙소의 특징만 적으세요!""",

    "ASK_ACCO_COMPLAIN_GENERAL" : """\n\n[시스템 지시사항]
    사용자가 기존 추천이 마음에 안 든다고 합니다. 
    [블랙리스트 규칙] 이전에 추천했던 숙소 목록 : [{shown_list}]
    1. 위 목록에 있는 숙소는 사용자가 이미 거절한 곳이므로 이번 검색 및 추천에서 절대 다시 등장해서는 안 됩니다.
    2. 사용자의 원래 조건인 '{acco_conditions}'을(를) 완벽히 유지한 채, `search_seoul_data`를 호출해 위 목록에 없는 완전히 새로운 숙소 3곳을 엄선하여 제안하세요. 
    3. chat_message에는 "제가 추천해 드린 숙소가 마음에 들지 않으셨군요 😅 사용자님의 취향과 조건을 바탕으로 새로운 숙소를 찾아올게요!" 라고 멘트하세요.
    [경고] 숙소 안내(reason) 필드에 교통수단, 환승, 소요 시간 등을 절대 상상해서 적지 마세요! 오직 숙소의 특징만 적으세요!""",

    "ASK_ACCO_UNLISTED_PROMPT" : """\n\n[시스템 지시사항 : 대본 100% 복사 출력]
    사용자가 기존 추천 리스트에 없는 외부 숙소를 언급했습니다. 절대 도구를 호출하거나 임의로 추천하지 말고 아래 대본만 100% 똑같이 출력하세요.
    <출력 대본>
    말씀하신 숙소는 제 추천 리스트에 없네요 😭 혹시 개인적으로 예약하신 곳인가요?
    </출력 대본>""",

    "ASK_ACCO_SPECIFIC_SEARCH" : """\n\n[시스템 지시사항: 특정 숙소 DB 검색]
    사용자가 리스트에 없는 특정 숙소명('{current_input}')을 지목했습니다.
    1. 즉시 `search_seoul_data` 도구를 호출하여 해당 숙소를 검색해 보세요.
    2. ✅ 검색 결과에 해당 숙소가 있다면, 추천 양식에 맞춰 해당 숙소를 안내하세요.
    3. ❌ 검색 결과에 없다면, 절대 임의의 다른 숙소를 추천하지 말고 아래 대본을 100% 똑같이 출력하세요!
    <출력 대본>
    말씀하신 숙소는 제 데이터에 없네요 😭 혹시 개인적으로 예약하신 곳인가요?
    </출력 대본>""",

    "ASK_ACCO_BOOKED" : """\n\n[시스템 지시사항 : 대본 100% 복사 출력]
    사용자가 예약을 완료했으므로 도구를 쓰지 말고 아래 대본만 똑같이 출력하세요.
    <출력 대본>
    오, 준비성이 대단하시네요! 😊 완벽한 동선 설계를 위해 예약하신 숙소 정보를 알려주세요.

    ✅ 숙소의 위치 (구 및 동 이름)
    ✅ 상호명
    ✅ 체크인 및 체크아웃 시간

    숙소 정보를 정확하게 알려주시면 더 완벽한 일정을 짜드릴 수 있어요 😊
    </출력 대본>""",

    "VERIFY_BOOKING_INFO" : """\n\n[시스템 지시사항: 예약 숙소 정보 검증 및 좌표 확보]
    사용자가 예약 정보를 입력했습니다. 아래 순서대로 '엄격하게' 행동하세요.
    1. 이전 대화 기록을 확인하여 [1.위치(구/동), 2.상호명, 3.체크인/아웃 시간] 3가지가 모두 파악되었는지 검사하세요.
    2. 하나라도 누락되었다면, 도구를 쓰지 말고 `chat_message`에 "앗! [누락된 항목] 정보가 빠져있네요 😭 이 정보를 알려주시면 완벽한 일정을 짜드릴게요!" 라고 작성하고 종료하세요.
    3. 3가지 정보가 모두 있다면, 반드시 `search_kakao_location` 도구를 호출하여 좌표를 검색하세요.
    4. 도구 검색 결과가 '검색 실패'인 경우 (매우 중요)
       - `accommodations` 필드는 무조건 `null`로 설정하세요. (절대 가상의 숙소 데이터를 지어내지 마세요!)
       - `chat_message`에 "말씀하신 숙소의 정확한 위치를 찾을 수 없어요 😭 상호명과 주소를 다시 한 번 확인해서 말씀해주세요." 라고 깔끔하게 한 문장만 작성하세요.
    5. ✅ 도구 검색이 성공했다면:
       - `chat_message`에 "✅ 숙소 위치 확인 완료!" 라고 작성하세요.
       - **중요: 검색된 숙소의 이름, 사용자가 말한 체크인/아웃 시간을 `accommodations` 필드(배열)에 딱 1개만 넣어서 함께 반환하세요.**
    * 경고 : 이 단계에서 절대 여러 개의 추천 리스트를 만들거나, 일정을 짜지 마세요!""",

    "ASK_WEATHER_PREF" : """\n\n[시스템 지시사항 : 대본 100% 복사 출력]
    절대 일정을 짜지 마세요! 아래 대본만 100% 똑같이 출력하세요.
    <출력 대본>
    앗! 꼼꼼히 확인해 보니 여행 일정 중에 **비/눈 예보가 있거나, 야외 활동이 힘들 만큼 덥거나 추운 날(폭염/한파)**이 껴있네요 😭
    해당 일자에 무리해서 야외를 돌아다니시면 너무 힘드실 텐데, 궂은 날씨가 예보된 **해당 날짜의 일정만** 특별히 실내 위주(미술관, 대형 복합 쇼핑몰 등)로 바꿔서 동선을 짜드릴까요?
    </출력 대본>""",
    
    "GENERATE_PLAN_WEATHER_APPLIED" : """\n\n[시스템 지시사항: 타임라인 생성 및 날씨 브리핑 작성 규칙]
    1. 날씨 브리핑 작성: 파이썬이 제공한 [AI 참조용 날씨 데이터]를 읽고, 해당 기간에 맞는 다정하고 센스 있는 조언을 `weather_tip` 필드에 작성하세요. (예: 맑고 더운 날씨라면 선크림과 양산 추천, 비 오면 우산, 쌀쌀하면 겉옷 등)
    2. (중요) 실내 일정 전환 기준: 미세먼지나 특보는 무시하고, 오직 **비/눈 소식**이 있거나 **폭염/한파** 기준에 해당하는 날짜만 실내 장소 위주로 타임라인을 구성하세요!
    3. `chat_message` 필드 주의사항: 사용자 화면에는 즉시 타임라인이 출력되도록 설계되어 있습니다. "일정 생성을 완료했습니다" 등의 뻔한 인사말이나 중복 안내 멘트를 절대 작성하지 마세요."""
}

@st.cache_resource
def get_agent_executor(model_label, current_step):
    config = MODEL_OPTIONS[model_label]
    
    if current_step == "GENERATE_PLAN": 
        active_tools = [search_seoul_data, search_seoul_reviews, get_comprehensive_route, get_subway_coordinates]
        iter_limit = 60
    elif current_step == "ASK_ACCO": 
        active_tools = [search_seoul_data, search_kakao_location]
        iter_limit = 15
    else: 
        active_tools = tools
        iter_limit = 25
        
    if "Pro" in model_label or "Deep" in model_label:
        temp = 0.2   
        max_t = 8192 
    else:
        temp = 0.1    
        max_t = 4096

    llm = ChatOpenAI(
        base_url=config["base_url"], 
        api_key=config["api_key"], 
        model=config["model"], 
        temperature=temp, 
        max_tokens=max_t
    )
    
    agent = create_tool_calling_agent(llm, active_tools, prompt)
    
    # 3. 실행 시간 및 에러 처리 강화
    return AgentExecutor(
        agent=agent, 
        tools=active_tools, 
        verbose=True, 
        handle_parsing_errors=True, 
        max_iterations=iter_limit, 
        max_execution_time=240 # 실행 시간을 5분으로 늘려 복잡한 일정 생성 보장
    )

# ==============================
# 대시보드 및 UI 렌더링 영역
# ==============================

def save_and_embed_memory(user_message, ai_message):
    os.makedirs("logs", exist_ok=True)
    log_filename = "logs/seoulmate_chat_logs.jsonl"
    log_data = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
        "user_input": user_message, 
        "ai_response": ai_message
    }
    
    # 단순히 파일에 한 줄 적고 끝냅니다 (속도 매우 빠름)
    with open(log_filename, "a", encoding="utf-8") as f: 
        f.write(json.dumps(log_data, ensure_ascii=False) + "\n")

def get_base64_bg(file_path):
    try:
        with open(file_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return ""

bg_base64 = get_base64_bg("seoulmate_background.png")
if bg_base64:
    st.markdown(f"""
        <style>
        /* 메인 배경 이미지 설정 */
        .stApp {{
            background-image: url("data:image/jpeg;base64,{bg_base64}");
            background-size: 100% auto;
            background-position: 70% bottom;
            background-attachment: fixed;
        }}
        /* 배경 가독성을 위한 어두운 오버레이 덧칠 */
        .stApp::before {{
            content: "";
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background-color: rgba(0, 0, 0, 0.6); /* 숫자가 클수록 어두워집니다 (0.6 추천) */
            z-index: -1;
        }}
        </style>
    """, unsafe_allow_html=True)

st.markdown("""
    <style>
    .stApp { background-color: transparent; }
    html, body { scroll-behavior: auto !important; }
    body { scrollbar-gutter: stable; }
    header[data-testid="stHeader"] { display: none !important; }

    /* 1. 채팅 입력창 컨테이너 (iMessage 스타일) */
    div[data-testid="stChatInput"] > div { 
        border-radius: 30px !important; 
        background-color: rgba(28, 28, 30, 0.85) !important; 
        backdrop-filter: blur(12px) !important;
        -webkit-backdrop-filter: blur(12px) !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important; 
        padding-right: 6px !important; /* 전송 버튼을 위한 여백 */
        box-shadow: 0 4px 30px rgba(0, 0, 0, 0.3) !important;
    }

    /* 2. 포커스 시 생기는 빨간색 네모 테두리 완벽 제거 */
    div[data-testid="stChatInput"] > div:focus-within, 
    div[data-testid="stChatInput"] > div *:focus {
        box-shadow: none !important;
        outline: none !important;
    }
    
    /* 포커스 시 은은한 하얀색 테두리로만 변경 */
    div[data-testid="stChatInput"] > div:focus-within {
        border-color: rgba(255, 255, 255, 0.4) !important; 
    }

    /* 3. iMessage 스타일 전송 버튼 (작고 파란 둥근 버튼) */
    button[data-testid="stChatInputSubmitButton"] {
        background-color: #0A84FF !important; 
        border-radius: 50% !important; 
        width: 34px !important;
        height: 34px !important;
        min-width: 34px !important;
        padding: 0 !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        transition: transform 0.2s ease, background-color 0.2s ease !important;
        margin-top: 4px !important;
        margin-bottom: 4px !important;
    }

    button[data-testid="stChatInputSubmitButton"]:hover {
        background-color: #007aff !important;
        transform: scale(1.05) !important;
    }
    button[data-testid="stChatInputSubmitButton"]:active {
        transform: scale(0.95) !important;
    }

    /* 4. 전송 버튼 안의 화살표 아이콘 흰색 처리 */
    button[data-testid="stChatInputSubmitButton"] svg {
        fill: #FFFFFF !important;
        color: #FFFFFF !important;
        width: 18px !important;
        height: 18px !important;
    }

    /* 5. 텍스트 입력 영역 (내부 껍데기 네모 박스 배경까지 완벽 투명화) */
    div[data-testid="stChatInput"] textarea {
        background-color: transparent !important; 
        color: #FFFFFF !important;
        caret-color: #0A84FF !important;
    }
    
    /* Streamlit이 텍스트박스 주변에 생성하는 내부 회색 wrapper들을 모두 투명하게 만듦 */
    div[data-testid="stChatInput"] div[data-baseweb="textarea"],
    div[data-testid="stChatInput"] div[data-baseweb="textarea"] > div {
        background-color: transparent !important;
        border: none !important;
        box-shadow: none !important;
    }

    /* 하단 컨테이너 배경 투명화 및 채팅창 넓이 동기화 */
    div[data-testid="stBottom"], div[data-testid="stBottom"] > div { 
        background-color: transparent !important; 
        background-image: none !important; 
    }
    div[data-testid="stBottomBlockContainer"] {
        background-color: transparent !important; 
        background-image: none !important; 
        max-width: 980px !important; /* 메인 화면과 채팅창 넓이 동기화 */
    }
    
    /* 채팅창 하단 여백에 자연스러운 어두운 그라데이션을 주어 글자와 입력창이 배경에 묻히지 않도록 보호 */
    div[data-testid="stBottom"] {
        background: linear-gradient(to top, rgba(0,0,0,0.95) 0%, rgba(0,0,0,0.5) 60%, rgba(0,0,0,0) 100%) !important;
        padding-top: 30px !important;
    }
            
    [data-testid="column"] { display: flex; align-items: center; }
    [data-testid="column"]:nth-of-type(1) { justify-content: flex-start; }
    [data-testid="column"]:nth-of-type(2) { justify-content: center; }
    [data-testid="column"]:nth-of-type(3) { justify-content: flex-end; }
    
    div[data-testid="column"]:nth-of-type(1) div[data-testid="stSelectbox"] div[data-baseweb="select"] {
        background-color: rgba(28, 28, 30, 0.5) !important; 
        backdrop-filter: blur(12px) !important;
        -webkit-backdrop-filter: blur(12px) !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important; 
        border-radius: 12px !important; 
        cursor: pointer !important; 
        padding: 0 8px !important;
        box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2) !important;
    }
    div[data-testid="column"]:nth-of-type(1) div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
        background-color: transparent !important; border: none !important;
    }
    div[data-testid="column"]:nth-of-type(1) div[data-testid="stSelectbox"] div[data-baseweb="select"] span { font-size: 14px !important; font-weight: 600 !important; color: #FFFFFF !important; }
    div[data-testid="column"]:nth-of-type(1) div[data-testid="stSelectbox"] div[data-baseweb="select"]:hover { background-color: rgba(255, 255, 255, 0.15) !important; border-color: rgba(255, 255, 255, 0.3) !important; }
    div[data-testid="column"]:nth-of-type(1) div[data-testid="stSelectbox"] svg { fill: #FFFFFF !important; }

    /* Clear 버튼 예전으로 원복 */
    div[data-testid="stVerticalBlock"]:has(#sticky-header-anchor) div[data-testid="column"]:nth-of-type(3) button {
        background-color: transparent !important; 
        color: #0A84FF !important; 
        border: none !important; 
        padding: 0px 10px !important; 
        font-weight: 500 !important; 
        font-size: 16px !important; 
        transition: all 0.2s ease !important; 
        box-shadow: none !important;
    }
    div[data-testid="stVerticalBlock"]:has(#sticky-header-anchor) div[data-testid="column"]:nth-of-type(3) button:hover { 
        color: #0056b3 !important; 
        background-color: rgba(10, 132, 255, 0.1) !important; 
        border-radius: 10px !important; 
    }
            
    div[data-testid="stDialog"] div[role="dialog"] { background-color: #1C1C1E !important; border: 1px solid #3A3A3C !important; border-radius: 20px !important; box-shadow: 0 10px 30px rgba(0,0,0,0.5) !important; }
    div[data-testid="stDialog"] h1 { color: #0A84FF !important; font-family: 'Pretendard', sans-serif !important; font-weight: 700 !important; }
    div[data-testid="stDialog"] label p { color: #AEAEB2 !important; font-size: 14px !important; }
    div[data-testid="stDialog"] button[kind="primary"] { background-color: #0A84FF !important; color: white !important; border: none !important; border-radius: 10px !important; padding: 10px !important; width: 100% !important; font-weight: 600 !important; }

    #planner-anchor { display: none; }
    div[data-testid="stElementContainer"]:has(#planner-anchor) + div[data-testid="stElementContainer"] button {
        background: rgba(255, 255, 255, 0.08) !important; color: #0A84FF !important; font-weight: 600 !important; font-size: 14px !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important; border-radius: 10px !important; padding: 4px 12px !important; min-height: auto !important; height: auto !important; line-height: 1.2 !important; margin-left: 46px !important; margin-top: 5px !important; margin-bottom: 20px !important;
    }

    .info-tooltip { position: relative; display: inline-block; cursor: pointer; margin-left: 6px; vertical-align: middle; }
    .info-icon { display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; border-radius: 50%; background-color: rgba(255, 255, 255, 0.2); color: #E5E5EA; font-size: 11px; font-weight: bold; font-style: italic; font-family: serif; }
    .info-tooltip .tooltip-text { visibility: hidden; width: max-content; max-width: 220px; background-color: #1C1C1E; color: #fff; text-align: center; border-radius: 8px; padding: 6px 12px; position: absolute; z-index: 1000; bottom: 140%; left: 50%; transform: translateX(-50%); font-size: 12px; font-weight: normal; box-shadow: 0 4px 12px rgba(0,0,0,0.5); opacity: 0; transition: opacity 0.2s ease, transform 0.2s ease; border: 1px solid #3A3A3C; white-space: pre-wrap; }
    .info-tooltip .tooltip-text::after { content: ""; position: absolute; top: 100%; left: 50%; margin-left: -5px; border-width: 5px; border-style: solid; border-color: #1C1C1E transparent transparent transparent; }
    .info-tooltip:hover .tooltip-text { visibility: visible; opacity: 1; transform: translateX(-50%) translateY(-3px); }  
    
    /* G버튼 */
    details > summary { list-style: none; }
    details > summary::-webkit-details-marker { display: none; }
    .gmap-btn:hover { background-color: #3367D6 !important; color: #FFFFFF !important; }
    .gmap-btn:focus, .gmap-btn:active { color: #3C4043 !important; }
    .gmap-btn:active { transform: scale(0.9) !important; }
    
    /* N버튼 */
    a.naver-route-btn {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    background-color: #03C75A !important;
    border: 1px solid #02b350 !important;
    height: 20px !important;
    padding: 0 6px !important;
    border-radius: 4px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1) !important;
    text-decoration: none !important;
    margin-left: 6px !important;
    vertical-align: middle !important; }
    a.naver-route-btn:hover { background-color: #02b350 !important; }
    a.naver-route-btn span.n-logo { font-weight: 900 !important; font-family: Arial, sans-serif !important; margin-right: 3px !important; color: #FFFFFF !important; }
    a.naver-route-btn span.n-text { color: #FFFFFF !important; font-size: 11px !important; font-weight: 600 !important; }
    </style>
""", unsafe_allow_html=True)

# 서울 대중교통 상징색 정의
TRANSIT_COLORS = {
    "1호선": "#0052A4", "2호선": "#00A84D", "3호선": "#EF7C1C", "4호선": "#00A4E3",
    "5호선": "#996CAC", "6호선": "#CD7C2F", "7호선": "#747F00", "8호선": "#E6186C",
    "9호선": "#BDB092", "수인분당": "#F5A200", "경의중앙": "#77C4A3", "신분당": "#D4003B",
    "공항철도": "#0090D2", 
    "간선": "#0068B7", "지선": "#00A84D", "광역": "#E6186C", "마을": "#53B332",
    "도보": "#8E8E93", "자차": "#0A84FF", "기본": "#E5E5EA"
}

def get_color_for_text(text):
    if not text: return TRANSIT_COLORS["기본"]
    for key, color in TRANSIT_COLORS.items():
        if key in text:
            return color
    return TRANSIT_COLORS.get("기본", "#E5E5EA")

def get_place_coords(place_name):
    if not place_name: return None
    clean_q = re.sub(r'복귀|휴식|짐\s*보관|체크인|체크아웃|및|주차|회수', '', place_name).strip()
    c_p = clean_q.replace(" ", "")
    
    def extract_gd(raw):
        if not raw or "정보" in raw: return ""
        p = [i.strip() for i in raw.split(',')]
        return f"{p[0]} {p[1]}" if len(p) > 1 else p[0]

    if "cached_places" in st.session_state:
        for cached_name, coords in st.session_state.cached_places.items():
            if c_p in cached_name.replace(" ", "") or cached_name.replace(" ", "") in c_p:
                # 🚨 [수정됨] "address" 필드 반환 추가!
                return {"name": cached_name, "lat": coords["lat"], "lon": coords["lng"], "address": coords.get("address", ""), "gd_kw": extract_gd(coords.get("gu_dong", ""))}
    try:
        docs = vector_store.similarity_search(clean_q, k=3)
        for d in docs:
            m = d.metadata
            if c_p in m.get('name', '').replace(" ", "") or m.get('name', '').replace(" ", "") in c_p:
                la, lo = m.get('lat') or m.get('y'), m.get('lng') or m.get('x')
                if la and lo:
                    return {"name": m.get('name'), "lat": float(la), "lon": float(lo), "address": m.get('address', ''), "gd_kw": extract_gd(m.get('gu_dong', ""))}
    except: pass
    return None

def render_snake_route_ui(route_text, naver_btn=""):
    route_text = route_text.replace("\n", " ").replace("📍", "").strip()
    
    summary = "이동 경로 안내"
    path = route_text
    
    if ":" in route_text:
        parts = route_text.split(":", 1)
        summary = parts[0].strip()
        path = parts[1].strip()
    elif "소요)" in route_text:
        parts = route_text.split("소요)", 1)
        summary = parts[0].strip() + "소요)"
        path = parts[1].strip()
        
    summary = re.sub(r'상세\s*경로\s*안내', '', summary).strip()
    
    path = re.sub(r'^\s*➔\s*', '', path) 
    path = re.sub(r'\(🚪.*?\)|\(빠른하차.*?\)', '', path)
    
    chunks = [c.strip() for c in path.split('➔') if c.strip()]
    node_data = []
    num_nodes = (len(chunks) + 1) // 2
    
    for i in range(num_nodes):
        node_chunk = chunks[i*2]
        edge_chunk = chunks[i*2 + 1] if i*2 + 1 < len(chunks) else None
        
        if edge_chunk: active_color = get_color_for_text(edge_chunk)
        else: active_color = get_color_for_text(chunks[i*2 - 1] if i > 0 else "")

        clean_node = re.sub(r'\s*체크인.*|\s*체크아웃.*|\s*복귀.*|\s*짐\s*보관.*|\s*주차.*|\s*차량\s*회수.*', '', node_chunk).strip()
        
        if "(" in clean_node:
            parts = clean_node.split("(", 1)
            part1 = parts[0].strip()
            part2 = parts[1].replace(")", "").strip()
            
            if "번" in part1:
                node_top, node_bot = part1, part2
            elif "번" in part2:
                node_top, node_bot = part2, part1
                
            elif "호선" in part1:
                node_top, node_bot = part2, part1
            elif "호선" in part2:
                node_top, node_bot = part1, part2
                
            else:
                node_top, node_bot = part1, part2
        else:
            node_top, node_bot = clean_node, ""

        edge_top, edge_bot = "", ""
        if edge_chunk:
            c_edge = edge_chunk.replace("🚇", "").replace("🚌", "").replace("🚶", "").replace("🚗", "").strip()
            if "도보" in edge_chunk: 
                edge_top, edge_bot = "도보", c_edge.replace("도보", "").strip(" ()")
            elif "자차" in edge_chunk: 
                edge_top, edge_bot = "자차 이동", c_edge.replace("자차 이동", "").strip(" ()")
            elif "방면" in edge_chunk: 
                parts = re.split(r'\(|,', c_edge)
                edge_top = parts[1].replace(")", "").strip() if len(parts) > 1 else "이동"
                edge_bot = parts[0].strip()
            else:
                if "(" in c_edge: 
                    edge_top, edge_bot = c_edge.split("(")[0].strip(), c_edge.split("(")[1].replace(")", "").strip()
                else: 
                    edge_top, edge_bot = c_edge, ""

        node_data.append({'row': i // 3, 'n_top': node_top, 'n_bot': node_bot, 'color': active_color, 'e_top': edge_top, 'e_bot': edge_bot, 'type': 'NONE' if i == num_nodes - 1 else 'DOWN' if (i+1)%3==0 else 'RIGHT' if (i//3)%2==0 else 'LEFT'})

    html = f"<div style='margin-bottom: 8px; font-size: 15px; color: #E5E5EA; display: flex; align-items: center;'>🔍 <b>상세 경로 안내</b> : {summary} {naver_btn}</div>"
    html += "<div style='padding: 20px 10px 10px 10px; background: transparent; display: flex; justify-content: center; overflow: hidden;'>"
    html += "<div style='display: flex; flex-direction: column; width: 100%; max-width: 420px;'>"
    
    rows = {}
    for item in node_data: rows.setdefault(item['row'], []).append(item)
        
    for r in range(len(rows)):
        flex_dir = "row-reverse" if r % 2 == 1 else "row"
        current_row_nodes = rows[r]
        padded_row = current_row_nodes.copy()
        while len(padded_row) < 3: padded_row.append({'is_ghost': True})
            
        html += f'<div style="display: flex; width: 100%; flex-direction: {flex_dir}; justify-content: space-between; margin-bottom: 30px;">' 
        
        for item in padded_row:
            if item.get('is_ghost'):
                html += '<div style="flex: 1; min-width: 0; visibility: hidden;"></div>'
                continue
                
            html += f'<div style="flex: 1; min-width: 0; position: relative; display: flex; flex-direction: column; align-items: center; box-sizing: border-box;">'
            
            # 글자 주변에 말풍선 배경색(#2C2C2E)과 똑같은 색의 두꺼운 테두리(그림자)를 생성하여 선을 자연스럽게 덮어버림
            halo_style = "text-shadow: 2px 0 0 #2C2C2E, -2px 0 0 #2C2C2E, 0 2px 0 #2C2C2E, 0 -2px 0 #2C2C2E, 1px 1px 0 #2C2C2E, -1px -1px 0 #2C2C2E, 1px -1px 0 #2C2C2E, -1px 1px 0 #2C2C2E;"
            
            # 텍스트 박스 높이를 55px로 고정하여 글자가 3줄이 되어도 동그라미를 밀어내지 않게 방어
            html += f'<div style="height: 55px; width: 100%; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; margin-bottom: 5px;">'
            html += f'<div style="max-width: 120px; font-weight: 800; font-size: 13px; color: #FFFFFF; text-align: center; word-break: keep-all; white-space: normal; line-height: 1.3; z-index: 10; {halo_style}">{item["n_top"]}</div>'
            html += f'</div>'
            
            # 동그라미 내부도 말풍선 배경색과 일치하도록 변경
            html += f'<div style="width: 16px; height: 16px; border-radius: 50%; border: 4px solid {item["color"]}; background: #2C2C2E; z-index: 2; box-shadow: 0 0 6px rgba(0,0,0,0.5);"></div>'
            
            bot_text = item["n_bot"] if item["n_bot"] else " "
            html += f'<div style="width: 100%; max-width: 120px; font-size: 11px; color: {item["color"]}; margin-top: 8px; font-weight: 600; text-align: center; word-break: keep-all; white-space: normal; line-height: 1.2; z-index: 10; {halo_style}">{bot_text}</div>'
            
            # 연결선 레이아웃 (경로 텍스트들에도 모두 외곽선 효과 적용)
            if item["type"] == "RIGHT":
                html += f'<div style="position: absolute; top: 66px; left: 50%; width: 100%; height: 4px; background: {item["color"]}; z-index: 1; border-radius: 2px;"></div>'
                html += f'<div style="position: absolute; top: 46px; left: 50%; width: 100%; text-align: center; font-size: 11px; color: {item["color"]}; font-weight: 700; white-space: nowrap; z-index: 3; {halo_style}">{item["e_top"]}</div>'
                html += f'<div style="position: absolute; top: 76px; left: 50%; width: 100%; text-align: center; font-size: 11px; color: #AEAEB2; white-space: nowrap; z-index: 3; {halo_style}">{item["e_bot"]}</div>'
            elif item["type"] == "LEFT":
                html += f'<div style="position: absolute; top: 66px; right: 50%; width: 100%; height: 4px; background: {item["color"]}; z-index: 1; border-radius: 2px;"></div>'
                html += f'<div style="position: absolute; top: 46px; right: 50%; width: 100%; text-align: center; font-size: 11px; color: {item["color"]}; font-weight: 700; white-space: nowrap; z-index: 3; {halo_style}">{item["e_top"]}</div>'
                html += f'<div style="position: absolute; top: 76px; right: 50%; width: 100%; text-align: center; font-size: 11px; color: #AEAEB2; white-space: nowrap; z-index: 3; {halo_style}">{item["e_bot"]}</div>'
            elif item["type"] == "DOWN":
                html += f'<div style="position: absolute; top: 74px; left: calc(50% - 2px); width: 4px; height: 115px; background: {item["color"]}; z-index: 1; border-radius: 2px;"></div>'
                html += f'<div style="position: absolute; top: 120px; right: calc(50% + 8px); font-size: 11px; color: {item["color"]}; font-weight: 700; white-space: nowrap; text-align: right; z-index: 3; {halo_style}">{item["e_top"]}</div>'
                html += f'<div style="position: absolute; top: 120px; left: calc(50% + 8px); font-size: 11px; color: #AEAEB2; white-space: nowrap; text-align: left; z-index: 3; {halo_style}">{item["e_bot"]}</div>'
            
            html += '</div>' 
        html += '</div>' 
    html += '</div></div>' 
    return html

def draw_message(text, role):
    if ">" in text:
        if re.search(r'^\s*>', text, re.MULTILINE):
            def make_box(match):
                content = re.sub(r'^\s*>\s?', '', match.group(0), flags=re.MULTILINE)
                return f'<div style="background-color: rgba(255,255,255,0.05); border-left: 4px solid #0A84FF; padding: 15px; margin: 10px 0; border-radius: 8px; font-size: 14px; line-height: 1.6;">{content}</div>'
            text = re.sub(r'(?:^\s*>.*\n?)+', make_box, text, flags=re.MULTILINE)

    html_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    html_text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2" target="_blank" style="color: #0A84FF; font-weight: bold; text-decoration: none;">\1</a>', html_text)
    html_text = re.sub(r'\(예\s*[:：]\s*(.*?)\)', r'<div class="info-tooltip"><div class="info-icon">i</div><span class="tooltip-text">예 : \1</span></div>', html_text)
    html_text = html_text.replace("###", "<br><b>").replace("\n", "<br>")
    html_text = re.sub(r'(<br>\s*){3,}', '<br><br>', html_text)

    if role == "user":
        st.markdown(f'''
            <div style="display: flex; justify-content: flex-end; margin-bottom: 12px;">
                <div style="background-color: #0A84FF; color: white; padding: 12px 18px; border-radius: 20px 20px 5px 20px; max-width: 75%; line-height: 1.5; font-size: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.2);">
                    {html_text}
                </div>
            </div>
        ''', unsafe_allow_html=True)
    else:
        st.markdown(f'''
            <div style="display: flex; justify-content: flex-start; align-items: flex-end; margin-bottom: 12px;">
                <div style="flex-shrink: 0; width: 36px; height: 36px; border-radius: 50%; background-color: #3A3A3C; display: flex; justify-content: center; align-items: center; margin-right: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.2);">
                    <span style="font-size: 20px;">🤖</span>
                </div>
                <div style="background-color: #2C2C2E; color: white; padding: 12px 18px; border-radius: 20px 20px 20px 5px; max-width: 75%; line-height: 1.5; font-size: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.2);">
                    {html_text}
                </div>
            </div>
        ''', unsafe_allow_html=True)

def handle_error_response(error_msg, current_input, log_prefix=""):
    draw_message(error_msg, "assistant")
    st.session_state.memory.add_ai_message(error_msg)
    save_and_embed_memory(current_input, f"{log_prefix} {error_msg}".strip())
    st.rerun()

if "step" not in st.session_state: st.session_state.step = "initial"
if "trip_info" not in st.session_state: st.session_state.trip_info = {}
if "show_planner_button" not in st.session_state: st.session_state.show_planner_button = False

welcome_msg = "안녕하세요! 서울 여행의 든든한 파트너, <b>SeoulMate</b>입니다. ☺️\n\n사용자님의 취향을 반영해 빈틈없는 서울 <b>여행 일정</b>을 짜보고 싶으신가요?\n 아니면 사용자님의 취향에 맞는 서울의 <b>특정 장소</b>를 추천받고 싶으신가요?\n\n원하시는 방향을 말씀해 주시면 바로 도와드릴게요!"

header_container = st.container()
with header_container:
    selected_label = "⚡ Fast"
    st.markdown("<div id='sticky-header-anchor'></div>", unsafe_allow_html=True)
    head_col1, head_col2, head_col3 = st.columns([0.7, 6, 0.7])
    
    with head_col1: 
        pass
        
    with head_col2: 
        if st.session_state.username != "guest" and st.session_state.get("custom_room_title"):
            st.markdown(f"""
            <div style='display: flex; flex-direction: column; align-items: center; justify-content: center;'>
                <div style='color: white; font-weight: 800; margin: 0; font-size: 44px; letter-spacing: -1.2px; line-height: 1.1;'>SeoulMate</div>
                <div style='color: #E5E5EA; font-size: 16px; font-weight: 700; margin-top: 4px;'>💬 {st.session_state.custom_room_title}</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown("<div style='color: white; font-weight: 800; margin: 0; font-size: 44px; letter-spacing: -1.2px; text-align: center; line-height: 1.1;'>SeoulMate</div>", unsafe_allow_html=True)
            
    with head_col3:
        if st.button("Clear", use_container_width=False):
            # 랭체인 대화 메모리 백지화 후 환영 인사만 다시 넣기
            st.session_state.memory.clear()
            st.session_state.memory.add_ai_message(welcome_msg)
        
            # 여행 진행 단계 및 기본 정보 싹 다 초기화
            st.session_state.step = "initial"          
            st.session_state.show_planner_button = False 
            st.session_state.trip_info = {}
        
         # 그동안 이 방에 쌓였던 각종 임시 기억들(취향, 숙소조건, 날씨 등) 삭제
            keys_to_clear = [
                "user_preferences", "acco_conditions", "shown_hotels_dict", "all_shown_hotels",
                "bad_weather_details", "indoor_preference", "weather_summary", 
                "acco_type", "cached_places", "response_cache", "pending_system_message", "selected_hotel_name", "travel_companion",
                "shown_places_dict", "all_shown_places", "place_conditions", "travel_style"
            ]
            for k in keys_to_clear:
                if k in st.session_state:
                    del st.session_state[k]

            st.session_state.focus_chat = True
            save_current_room() 
            st.rerun()

agent_executor = get_agent_executor(selected_label, st.session_state.step)
st.markdown("<div style='height: 40px;'></div>", unsafe_allow_html=True)

if "memory" not in st.session_state:
    st.session_state.memory = ChatMessageHistory()
    st.session_state.memory.add_ai_message(welcome_msg)
    st.rerun()

def render_planner_button():
    st.markdown('''
        <style>
        #planner-anchor { display: none; } 
        div[data-testid="stElementContainer"]:has(#planner-anchor) + div[data-testid="stElementContainer"] button { 
            background-color: rgba(255, 255, 255, 0.04) !important; /* 💡 탁해 보이던 배경 투명도를 극단적으로 낮춤 */
            background-image: none !important; /* 💡 기본 버튼의 탁한 회색 그라데이션 완벽 제거 */
            backdrop-filter: blur(8px) !important; 
            -webkit-backdrop-filter: blur(8px) !important; 
            color: #0A84FF !important; 
            font-weight: 600 !important; 
            font-size: 15px !important; 
            border: 1px solid rgba(255, 255, 255, 0.1) !important; /* 테두리도 은은하게 */
            border-radius: 20px !important; 
            padding: 8px 24px !important; 
            margin-left: 46px !important; 
            margin-top: 5px !important; 
            margin-bottom: 20px !important; 
            box-shadow: none !important; 
            transition: all 0.2s ease !important; 
        } 
        div[data-testid="stElementContainer"]:has(#planner-anchor) + div[data-testid="stElementContainer"] button:hover { 
            background-color: rgba(255, 255, 255, 0.1) !important; 
            border-color: rgba(255, 255, 255, 0.3) !important; 
        } 
        </style>
        <div id="planner-anchor"></div>
    ''', unsafe_allow_html=True)
    if st.button("🗓️ **기본 정보 입력**", use_container_width=False, key="planner_btn"): show_info_form()

# 대화창 구성
chat_container = st.container()

with chat_container:
    # 과거 대화 기록 렌더링
    for msg in st.session_state.memory.messages:
        if "[시스템" not in msg.content:
            draw_message(msg.content, "user" if msg.type == "human" else "assistant")
            
    # 플래너 버튼 렌더링
    if st.session_state.show_planner_button:
        render_planner_button()

user_input = st.chat_input("서울의 궁금한 장소나, 여행 계획을 입력해 주세요.")
pending_msg = st.session_state.get("pending_system_message", None)

current_input = None
if pending_msg and not user_input:
    current_input = pending_msg
    del st.session_state["pending_system_message"]
elif user_input:
    current_input = user_input
    if "pending_system_message" in st.session_state:
        del st.session_state["pending_system_message"]

if current_input:
    # 시스템 메시지가 아닌, 진짜 사용자 입력만 말풍선으로 그리기
    if user_input and not current_input.startswith("[시스템]"):
        st.session_state.memory.add_user_message(user_input)
        save_current_room()
        with chat_container:
            draw_message(user_input, "user")

if current_input:
    clean_input = current_input.replace(" ", "") 
    COMPLAINT_KWS = ["별로", "별론", "별루", "다른", "다시", "이상", "왜", "틀렸", "안", "아닌", "섞여", "새로", "딴거", "구려", "바꿔", "말고", "빼고", "없는데", "아니잖아", "원하지", "않", "요청한적"]
    is_system_msg = current_input.startswith("[시스템")
    
    # 1. 동행자 및 이동 스타일 정밀 파악
    if not is_system_msg:
        # 동행자 파악
        companion_match = re.search(r'(아이|애기|연인|커플|부모님|엄마|아빠|가족|친구|혼자|부부|홀로|솔로)', current_input)
        if companion_match:
            st.session_state.travel_companion = companion_match.group(1)

        # 이동 스타일 정밀 필터링 및 표준화
        slow_kws = ["느긋", "여유", "슬렁", "천천히", "힐링", "쉬엄", "휴식", "한적", "안바쁘게", "안 바쁘게"]
        fast_kws = ["빡세", "많이", "부지런", "이곳저곳", "알차게", "타이트", "구석구석", "전투적", "바쁘게", "여러"]
        mod_kws = ["적당", "보통", "무난", "밸런스", "골고루"]

        if any(kw in current_input for kw in slow_kws):
            st.session_state.travel_style = "느긋하게"
        elif any(kw in current_input for kw in fast_kws):
            st.session_state.travel_style = "빡세게"
        elif any(kw in current_input for kw in mod_kws):
            st.session_state.travel_style = "적당히"
    
    # 의도 파악 (시스템 메시지일 때는 의도 파악 건너뛰기)
    is_planning_intent = False
    is_place_intent = False
    is_direct_place_search = False
    is_delegating = False
    
    if not is_system_msg:
        is_planning_intent = any(kw in current_input for kw in ["일정", "계획", "며칠", "코스", "타임라인", "서울 여행", "박", "짜줘", "만들어"])
        is_place_intent = any(kw in current_input for kw in ["장소", "핫플", "맛집", "카페", "추천", "어디가", "가볼만한", "동네", "놀곳"]) and not is_planning_intent
        is_delegating = any(kw in clean_input for kw in ["알아서", "아무거나", "상관없어", "마음대로", "편한대로"])
        
        # "홍대 카페", "서울 핫플" 등 구체적 명사나 핫플 단어가 같이 들어오면 '다이렉트 추천'으로 간주
        direct_kws = ["홍대", "성수", "강남", "종로", "이태원", "명동", "연남", "을지로", "핫플", "맛집", "카페", "조용한", "깔끔한"]
        if is_place_intent and any(kw in current_input for kw in direct_kws):
            is_direct_place_search = True

    pre_msg = ""
    dynamic_instruction = ""
    spinner_msg = "**SeoulMate가 타이핑 중입니다.**"
    
    is_complaining = False
    is_positive_answer = False
    is_negative_answer = False
    selected_hotel = None
    just_transitioned_to_acco = st.session_state.pop("just_transitioned_to_acco", False)
    is_verifying_booking = False
    
    # 모드 강제 전환 우선 감지 (일정 <-> 장소)
    if is_planning_intent and st.session_state.step not in ["ASK_WEATHER_PREF", "GENERATE_PLAN", "ASK_STYLE", "ASK_ACCO"]:
        st.session_state.step = "initial"
        reset_search_context() # 일정 모드로 넘어가면 장소 블랙리스트 소멸
        st.session_state.show_planner_button = True
        dynamic_instruction = STEP_SCRIPTS["initial"]
        
    # 구체적 조건이 있는 다이렉트 장소 추천
    elif st.session_state.step == "initial" and is_direct_place_search:
        st.session_state.step = "PLACE_RECOMMENDED" # 질문 스킵하고 추천으로 직행
        st.session_state.place_conditions = current_input # "홍대 조용한 카페" 조건 영구 기억
        dynamic_instruction = STEP_SCRIPTS["SEARCH_PLACE"]
        spinner_msg = "**SeoulMate가 장소를 검색 중입니다.**"
        
    # 정보가 부족한 일반 장소 추천
    elif st.session_state.step == "initial" and is_place_intent:
        st.session_state.step = "ASK_PLACE_PREF"
        dynamic_instruction = STEP_SCRIPTS["ASK_PLACE_PREF"]

    # 일반 로직에서의 장소 추천 루프
    elif st.session_state.step == "ASK_PLACE_PREF":
        if is_system_msg: 
            dynamic_instruction = STEP_SCRIPTS["ASK_PLACE_PREF"]
        else:
            st.session_state.place_conditions = st.session_state.get("place_conditions", "") + " " + current_input
            
            cond_words = len(st.session_state.place_conditions.split())
            cond_length = len(st.session_state.place_conditions.replace(" ", "")) # 공백 제외 순수 글자수
            
            # 단어가 2개 이상이거나, 순수 글자수가 8자 이상이면 충분한 정보로 간주 (또는 위임했을 때)
            if (cond_words >= 2 or cond_length >= 8) or is_delegating:
                if is_delegating: st.session_state.place_conditions = "서울 핫플"
                
                st.session_state.step = "PLACE_RECOMMENDED"
                dynamic_instruction = STEP_SCRIPTS["SEARCH_PLACE"]
                spinner_msg = "**SeoulMate가 장소를 검색 중입니다.**"
            else:
                dynamic_instruction = STEP_SCRIPTS["ASK_PLACE_PREF_AGAIN"]
                spinner_msg = "**SeoulMate가 타이핑 중입니다.**"

    # 추천 후 불만 처리
    elif st.session_state.step == "PLACE_RECOMMENDED":
        is_complaining = any(kw in clean_input for kw in COMPLAINT_KWS) or any(kw in clean_input for kw in ["다른", "더", "말고", "딴거"])
        if is_complaining:
            # 방금 보여준 장소를 블랙리스트에 담기
            if "all_shown_places" not in st.session_state: st.session_state.all_shown_places = set()
            st.session_state.all_shown_places.update(st.session_state.get("shown_places_dict", {}).keys())
            st.session_state.shown_places_dict = {}

            # 기존 조건에 사용자의 새로운 요청을 누적해서 기억
            st.session_state.place_conditions = st.session_state.get("place_conditions", "") + " " + current_input
            
            # 원래 조건(place_conditions)을 유지한 채로 블랙리스트 적용하여 재검색
            shown_list_str = ", ".join(st.session_state.all_shown_places) if st.session_state.all_shown_places else "없음"
            conds = st.session_state.place_conditions
            
            dynamic_instruction = STEP_SCRIPTS["COMPLAIN_PLACE"].format(shown_list=shown_list_str, place_conditions=conds)
            spinner_msg = "**SeoulMate가 새로운 장소를 검색 중입니다.**"

    # 일정 생성: 스타일 파악 루프
    elif st.session_state.step == "ASK_STYLE":
        if is_system_msg:
            ti = st.session_state.trip_info
            dynamic_instruction = STEP_SCRIPTS["ASK_STYLE"].format(dates=ti.get('dates', ''), duration=ti.get('duration', ''), transport=ti.get('transport', ''), arrival_loc=ti.get('arrival_loc', ''), arrival_time=ti.get('arrival_time', ''))
        else:
            st.session_state.user_preferences = st.session_state.get("user_preferences", "") + " " + current_input
            
            if len(st.session_state.user_preferences) >= 10 or is_delegating:
                st.session_state.step = "ASK_ACCO"
                dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_FLOW_1"]
                just_transitioned_to_acco = True
            else:
                dynamic_instruction = STEP_SCRIPTS["ASK_STYLE_CHECK"]

    # 일정 생성: 숙소 파악 루프
    elif st.session_state.step == "ASK_ACCO":
        if just_transitioned_to_acco:
            dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_FLOW_1"]
        else:
            last_ai_msg = next((m.content for m in reversed(st.session_state.memory.messages) if m.type == "ai" and "[시스템" not in m.content), "")
            is_booked_intent = any(kw in clean_input for kw in ["예약했", "예약함", "예약완료", "잡았", "이미", "미리", "정했", "있어"])
            is_negative_answer = (any(current_input.strip().startswith(kw) for kw in ["아니", "ㄴㄴ", "노"]) or any(kw in clean_input for kw in ["안했", "아직", "없어"])) and not is_booked_intent
            is_positive_answer = any(current_input.strip().startswith(kw) for kw in ["응", "어", "네", "맞아", "ㅇㅇ", "예", "그렇"]) or is_booked_intent
            
            asked_acco_type = "숙소 타입" in last_ai_msg or "분위기" in last_ai_msg
            asked_existence = "예약해두셨나요" in last_ai_msg or "예약하셨나요" in last_ai_msg
            asked_external = "제 데이터에 없네요" in last_ai_msg or "추천 리스트에 없네요" in last_ai_msg
            asked_booking_info = any(kw in last_ai_msg for kw in ["숙소 정보를 알려주세요", "찾을 수 없", "빠져있네요", "확인해 주"])

            is_complaining = any(kw in clean_input for kw in COMPLAINT_KWS)
            acco_types = ["호스텔", "모텔", "호텔", "게하", "게스트하우스", "한옥", "펜션", "민박", "에어비앤비"]
            is_type_complaint = any(t in current_input for t in acco_types) and (is_complaining or is_negative_answer or "없" in current_input)
            if is_type_complaint: is_complaining = True
            if asked_external and is_negative_answer: is_complaining = True

            has_time_format = bool(re.search(r'\d{1,2}:\d{2}', current_input)) or bool(re.search(r'\d{1,2}시', current_input))
            has_acco_params = asked_acco_type and not is_complaining and not is_negative_answer

            shown_hotels = list(st.session_state.get("shown_hotels_dict", {}).keys())
            selected_hotel = None
            
            if len(shown_hotels) >= 1 and any(kw in clean_input for kw in ["1번", "일번", "첫번", "1"]): selected_hotel = shown_hotels[0]
            elif len(shown_hotels) >= 2 and any(kw in clean_input for kw in ["2번", "이번", "두번", "2"]): selected_hotel = shown_hotels[1]
            elif len(shown_hotels) >= 3 and any(kw in clean_input for kw in ["3번", "삼번", "세번", "3"]): selected_hotel = shown_hotels[2]
            
            if not selected_hotel:
                for hotel in sorted(shown_hotels, key=len, reverse=True):
                    clean_hotel = re.sub(r'\(.*?\)', '', hotel).replace(" ", "")
                    core_name = re.sub(r'호텔|게스트하우스|게하|펜션|스테이|민박|모텔|레지던스', '', clean_hotel)
                    if (len(core_name) >= 2 and core_name in clean_input) or (len(clean_hotel) >= 2 and clean_hotel in clean_input):
                        selected_hotel = hotel; break

            if not selected_hotel and "all_shown_hotels" in st.session_state:
                for past_hotel in sorted(st.session_state.all_shown_hotels, key=len, reverse=True):
                    clean_past = re.sub(r'\(.*?\)', '', past_hotel).replace(" ", "")
                    core_past = re.sub(r'호텔|게스트하우스|게하|펜션|스테이|민박|모텔|레지던스', '', clean_past)
                    if (len(core_past) >= 2 and core_past in clean_input) or (len(clean_past) >= 2 and clean_past in clean_input):
                        selected_hotel = past_hotel
                        h_meta = st.session_state.get("cached_places", {}).get(past_hotel, {})
                        if "shown_hotels_dict" not in st.session_state: st.session_state.shown_hotels_dict = {}
                        st.session_state.shown_hotels_dict[past_hotel] = {"name": past_hotel, "check_in_out": h_meta.get("check_in_out", "오후 3:00 / 오전 11:00"), "lat": h_meta.get("lat"), "lon": h_meta.get("lng")}
                        break

            is_unlisted_mention = len(shown_hotels) > 0 and not selected_hotel and not is_complaining and not has_acco_params and not is_positive_answer and not is_negative_answer and not asked_booking_info and not asked_external and not is_delegating

            if selected_hotel and not is_complaining:
                st.session_state.selected_hotel_name = selected_hotel
                reset_search_context()
                base_msg = f"탁월한 선택이세요! 사용자님의 취향에 딱 맞는 맞춤 일정을 짜서 얼른 돌아올게요 🗓️"
                st.session_state.memory.add_ai_message(base_msg)
                
                with chat_container:
                    draw_message(base_msg, "assistant")

                with st.spinner("**SeoulMate가 여행 기간의 날씨를 조회하고 있습니다.**"):
                    weather_summary, bad_weather_details = SeoulMateWeather().check_trip_weather(st.session_state.trip_info.get("dates", "2026-03-01 ~ 2026-03-03"))
                
                st.session_state.weather_summary = weather_summary
                st.session_state.bad_weather_details = bad_weather_details
                st.session_state.acco_type = "SELECTED"
                
                if bad_weather_details:
                    st.session_state.step = "ASK_WEATHER_PREF"
                    dates_str = ", ".join([item[0] for item in bad_weather_details])
                    raw_reasons = []
                    for item in bad_weather_details: raw_reasons.extend(item[1].split(" 및 "))
                    reasons_str = ", ".join(list(dict.fromkeys(raw_reasons)))
                    ask_msg = f"앗! 그런데 일기예보를 조회해 보니 **'{dates_str}'**에 **{reasons_str}** 예보가 있네요 🧐 해당 날짜의 일정은 실내 동선 위주로 짜드릴까요?"
                    st.session_state.memory.add_ai_message(ask_msg)
                    save_and_embed_memory(current_input, base_msg + "\n\n" + ask_msg)
                    save_current_room()
                    st.rerun()
                else:
                    st.session_state.step = "GENERATE_PLAN" 
                    st.session_state.pending_system_message = f"[시스템 긴급] '{selected_hotel}' 확정. 날씨 이상 없음. 다음 단계를 진행하세요."
                    save_and_embed_memory(current_input, base_msg)
                    save_current_room()
                    st.rerun()

            if has_acco_params: st.session_state.acco_conditions = current_input
            elif is_delegating and "acco_conditions" not in st.session_state: st.session_state.acco_conditions = "대중적인 가성비 숙소"
            acco_cond = st.session_state.get("acco_conditions", "기본 숙소 조건")

            if is_complaining:
                pre_msg = "앗, 원하시지 않는 숙소 타입이 섞여 들어갔네요! 😭 조건에 맞는 숙소를 다시 찾아올게요 🔍" if is_type_complaint else "앗, 제가 놓친 부분이 있나 보네요! 조건에 맞춰 새로운 곳을 꼼꼼하게 다시 찾아볼게요 🧐"
                spinner_msg = "**SeoulMate가 새로운 숙소를 검색 중입니다.**"
            elif has_acco_params or is_delegating: spinner_msg = "**SeoulMate가 숙소를 검색 중입니다.**"
            elif asked_booking_info: spinner_msg = "**SeoulMate가 숙소 정보를 확인 중입니다.**"
            
            wants_recommendation = any(kw in clean_input for kw in ["추천", "부탁", "골라", "없어", "아직", "안했"])
            
            if not any("예약해두셨나요" in m.content for m in st.session_state.memory.messages if m.type == "ai"): 
                dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_FLOW_1"]
            elif (has_time_format or asked_booking_info) and not is_negative_answer and not is_complaining: 
                dynamic_instruction = STEP_SCRIPTS["VERIFY_BOOKING_INFO"]
                is_verifying_booking = True
            elif is_complaining:
                if "all_shown_hotels" not in st.session_state: st.session_state.all_shown_hotels = set()
                combined_blacklist = st.session_state.all_shown_hotels.union(st.session_state.get("shown_hotels_dict", {}).keys())
                shown_list_str = ", ".join(combined_blacklist) if combined_blacklist else "없음"
                dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_COMPLAIN_TYPE"].format(acco_conditions=acco_cond, shown_list=shown_list_str) if is_type_complaint else STEP_SCRIPTS["ASK_ACCO_COMPLAIN_GENERAL"].format(acco_conditions=acco_cond, shown_list=shown_list_str)
            elif is_unlisted_mention: 
                dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_UNLISTED_PROMPT"]
            elif wants_recommendation or (asked_existence and is_negative_answer): 
                dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_FLOW_2"]
            elif has_acco_params or is_delegating: 
                dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_SEARCH"]
            elif asked_existence and is_positive_answer: 
                dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_BOOKED"]
            elif asked_external and is_positive_answer: 
                dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_BOOKED"]
            else: dynamic_instruction = STEP_SCRIPTS["ASK_ACCO_SPECIFIC_SEARCH"].format(current_input=current_input)

    # 일정 생성: 날씨 묻기 분기
    elif st.session_state.step == "ASK_WEATHER_PREF":
        if not is_system_msg:
            if any(kw in clean_input for kw in ["아니", "ㄴㄴ", "노", "괜찮", "그냥", "원래", "기존", "됐어", "하지마"]): is_positive_weather = False
            else: is_positive_weather = any(current_input.strip().lower().startswith(kw) for kw in ["응", "어", "네", "맞", "ㅇㅇ", "예", "그렇", "그래", "콜", "ok", "알겠", "알았", "ㅇㅋ"]) or any(kw in clean_input for kw in ["바꿔", "실내", "좋", "부탁", "해줘", "변경", "오케이"])
            
            st.session_state.step = "GENERATE_PLAN"
            st.session_state.indoor_preference = is_positive_weather
            bad_dates = ", ".join([item[0] for item in st.session_state.get('bad_weather_details', [])])
            
            pre_msg = f"알겠습니다. **'{bad_dates}'**는 실내 장소 위주로 일정을 짜볼게요! ⏳" if is_positive_weather else "알겠습니다. 기존 취향대로 멋진 일정을 짜올게요! ⏳"
            st.session_state.memory.add_ai_message(pre_msg)
            st.session_state.pending_system_message = f"[시스템 긴급 명령] 실내 일정 선호도: {is_positive_weather}. 즉시 일정을 생성하세요."
            save_current_room()
            st.rerun()

    elif st.session_state.step == "GENERATE_PLAN":
        spinner_msg = "**SeoulMate가 타이핑 중입니다.**"
        acco_type = st.session_state.get("acco_type", "SEARCHED")
        user_prefs = st.session_state.get("user_preferences", "사용자 맞춤 테마")
        ti = st.session_state.trip_info
        selected_hotel_name = st.session_state.get("selected_hotel_name", "확정된 숙소")
        hotel_meta = st.session_state.get("shown_hotels_dict", {}).get(selected_hotel_name, {})
        hotel_cio = hotel_meta.get("check_in_out", "오후 3:00 / 오전 11:00")
        checkout_time = hotel_cio.split('/')[-1].strip() if '/' in hotel_cio else "오전 11:00"
        h_lat, h_lng = hotel_meta.get("lat"), hotel_meta.get("lon")

        if not h_lat and "cached_places" in st.session_state and selected_hotel_name in st.session_state.cached_places:
            h_lat = st.session_state.cached_places[selected_hotel_name]["lat"]
            h_lng = st.session_state.cached_places[selected_hotel_name]["lng"]

        coord_info = f"위도 {h_lat}, 경도 {h_lng}" if h_lat and h_lng else "좌표 정보 없음 (필요 시 검색 도구 활용)"
        w_summary = st.session_state.get("weather_summary", "날씨 정보 없음")
        is_indoor = st.session_state.get("indoor_preference", False)
        bad_dates = ", ".join([item[0] for item in st.session_state.get('bad_weather_details', [])])
        indoor_cmd = f"""\n[실내 일정 반영 규칙] 반드시 **[{bad_dates}]** 해당 날짜의 일정만 실내 명소 위주로 안전하게 구성하세요.""" if is_indoor and bad_dates else ""
        dates_str = ti.get('dates', '2026-01-01 ~ 2026-01-02')
        try:
            start_str = dates_str.split("~")[0].strip()
            s_date = datetime.strptime(start_str, "%Y-%m-%d")
            days_kr = ["월", "화", "수", "목", "금", "토", "일"]
            yoil_list = [f"{i+1}일차({(s_date + timedelta(days=i)).strftime('%m/%d')} {days_kr[(s_date + timedelta(days=i)).weekday()]}요일)" for i in range(ti.get('duration', 1))]
            yoil_context = ", ".join(yoil_list)
        except:
            yoil_context = "요일 정보 없음"
        
        # 숙소 주차 정보 추출
        hotel_parking_info = hotel_meta.get("parking", "주차 정보 없음")
        if hotel_parking_info in ["없음", "불가", "정보 없음", "nan"]:
            parking_status = "주차 불가 (근처 공영주차장 이용 안내 필요)"
        else:
            parking_status = f"주차 가능 ({hotel_parking_info})"

        # 파이썬 변수를 명시적으로 꺼내서 AI가 인식하게 함
        t_style = st.session_state.get('travel_style', '적당히')
        t_comp = st.session_state.get('travel_companion', '없음')
        t_theme = st.session_state.get('user_preferences', '일반 관광')

        # 4일 이상 장기 일정일 경우 AI에게 텍스트 압축 강제 명령
        trip_days = ti.get('duration', 1)
        compression_cmd = ""
        if trip_days >= 4:
            compression_cmd = "\n🚨 [장기 일정 출력 토큰 방어 모드 발동!] 일정이 매우 깁니다! JSON 출력이 끊기는 것을 막기 위해 모든 스케줄의 `basic_info`와 `guide_tip`을 절대 길게 쓰지 말고 딱 '1문장'으로 극단적으로 요약해서 작성하세요. 하루 방문 장소도 핵심 위주로 최소화하세요."

        strategic_mission = f"""
        \n\n[가이드 복합 설계 알고리즘 (최우선 반영)]
        당신은 아래의 요소를 복합적으로 계산하여 최적의 타임라인을 짜야 합니다.
        - 거점 데이터 : 숙소({selected_hotel_name}) | 체크인({hotel_cio}) | 주차({parking_status})
        - 교통 수단 : {ti.get('transport', '대중교통')}
        - 여행테마 : {t_theme}
        - 동행자 : {t_comp}
        - 이동스타일 : {t_style}
        
        ▶ 🎯 [장소 선정 핵심 공식: 테마(What) x 이동(Where) x 동행자(How)]
           - 1. [방향성 설정]: 사용자가 요청한 '여행 테마'와 '이동 스타일'을 최우선으로 하여 방문할 핵심 동네를 결정하세요.
           - 2. [동행자 맞춤 필터링]: 결정된 동네 내에서 '동행자'의 특성을 제약 조건으로 삼아 장소를 최종 선별하세요. (예: 아이 동반 시 평지/유모차, 부모님 동반 시 웨이팅 없는 한식, 연인 동반 시 야경/분위기 위주)
        
        🚨 [타임라인 꼬리물기 절대 원칙 (환각 및 순간이동 방지)]
        1. [N번째 스케줄의 도착 장소(place_name)]는 무조건 [N+1번째 스케줄의 출발지(route_detail의 첫 번째 📍)]가 되어야 합니다. 
        2. 만약 백년옥 ➔ 봉은사로 이동했다면, 다음 스케줄의 출발지는 반드시 '봉은사'여야 합니다. 이를 어기면 시스템 오류가 발생합니다.
        3. 식당 및 카페 누락 금지: 이동 경로만 적고 식사/티타임을 건너뛰는 에러를 절대 범하지 마세요. 밥을 먹거나 카페에 가는 것도 반드시 독립된 하나의 `ScheduleItem` 블록으로 작성하여 `place_time`과 `place_name`을 명시해야 합니다.
        4. 🚨 [시간 공백 절대 금지]: 스케줄과 스케줄 사이에 비어있는 시간이 없어야 합니다! 앞 스케줄이 19:30에 끝났다면, 다음 스케줄(이동 또는 다음 장소)은 반드시 19:30에 시작해야 합니다. 저녁 식사 후 복귀(22:00) 전까지 시간이 붕 뜬다면, 야경 명소나 산책 일정을 추가해 시간을 완벽하게 꼬리물기로 채우세요!

        🚨 [최우선 시간 규칙 - 무조건 22시 꽉 채우기]
        어떤 동행자나 이동 스타일이든, 마지막 날을 제외한 모든 일차의 스케줄은 **반드시 오후 21:30 ~ 22:00 사이**에 숙소로 복귀하는 일정으로 끝나야 합니다! 저녁 7시나 8시에 일찍 일정을 끝내는 것을 절대 금지합니다. 저녁 식사 후 시간이 남는다면 야경 명소, 산책 등의 장소 방문을 통해 야간 일정을 무조건 추가하여 시간을 꽉 채우세요!

        🚨 [이동 스타일 및 동행자에 따른 타임라인 밀도 규칙]
        파악된 '이동 스타일(느긋/적당/빡세게)'과 '동행자'에 맞춰 하루 일정 개수를 완벽히 통제하세요.
        * 🐢 [느긋 / 여유 / 아이 동반 / 부모님 동반] : 장소 간 이동을 최소화하고 체류 시간을 2.5시간 이상 넉넉히 잡으세요.
        * 🚶 [적당 / 보통] : 체류 시간 1.5시간 내외로 잡으세요
        * 🔥 [빡세 / 많이 / 부지런히 / 이곳저곳] : 체류 시간을 1시간으로 잡으며 다양한 명소를 꽉 채우세요.

        🚨 [이동 스타일별 동선(권역) 범위 지능적 설정 규칙]
        `search_seoul_data` 도구가 찾아준 장소들의 **'주소(구/동)'** 데이터를 반드시 확인하여, 동선이 낭비되지 않게 지능적으로 배치하세요!
        * 🐢 [느긋 / 아이 동반 / 부모님 동반] : 철저한 **"1일 1구 집중 원칙"** 적용. 밥도 카페도 메인 관광지와 완전히 같은 동네에서만 찾아 이동 시간을 최소화하세요.
        * 🚶 [적당 / 보통] : 하루에 **2~3개 인접 구(Gu)** 정도의 이동은 허용합니다. (예: 종로구에서 놀다가 인접한 중구로 이동하여 저녁 식사)
        * 🔥 [빡세 / 이곳저곳] : 강남과 강북을 넘나드는 **광역 이동을 적극 허용**합니다! 거리가 멀어도 핫플이라면 타임라인에 넣으세요. 단, 무의미하게 오가는 최악의 지그재그 동선(예: 강남 ➔ 종로 ➔ 다시 강남)은 피하고, 한 방향으로 매끄럽게 이어지도록 짜세요.

        🚨 [타임라인 물리 법칙 및 네이밍 규격 (UI 연동용 절대 규칙)]
        1. [1일차 시작 시나리오 : 시간 및 교통수단 복합 분기]
           도착시간({ti.get('arrival_time')})과 체크인시간({hotel_cio})을 비교하여 아래 규칙을 엄격히 따르세요.

           🅰️ [자차(직접 운전) 유저] : "서울 도착 = 숙소 도착"가 기본입니다.
             - (공통) 첫 스케줄은 반드시 move_time을 "해당 없음"으로 적어 ➔ 기호 생성을 차단하세요.
             - (Case 1: 일찍 도착) 도착 < 체크인 : 
                * 첫 일정 : place_name을 "{selected_hotel_name} 짐 보관"로 작성. (basic_info 생략, 가이드팁에 주차 안내 작성). 
                * 이후 흐름 : 남은 시간이 1시간 이내면 '도보권' 일정을, 1시간 이상이면 다시 '차를 타고' 외부 일정을 진행한 뒤 오후에 별도의 '체크인' 스케줄을 추가하세요.
             - (Case 2: 늦게 도착) 도착 >= 체크인 :
                * 첫 일정 : place_name을 "{selected_hotel_name} 체크인 및 주차"로 작성. (이때만 basic_info에 숙소 설명 작성)

           🅱️ [대중교통 유저] : "도착 ➔ 숙소로 이동 ➔ 짐 보관" 과정이 반드시 노출되어야 합니다.
             - (공통) 첫 번째 스케줄은 반드시 [{ti.get('arrival_loc')} ➔ {selected_hotel_name}] 이동이어야 합니다! (move_time에 실제 이동시간 기재, route_detail 필수, place_time은 "해당 없음")
             - (Case 1: 일찍 도착) 도착 < 체크인 :
                * 두 번째 스케줄로 "{selected_hotel_name} 짐 보관"을 작성 (move_time "해당 없음", place_time에 짐 보관 시간 기재).
                * 이후 남은 시간에 따라 외부 일정을 진행하고 오후에 별도의 '체크인' 스케줄을 추가하세요.
             - (Case 2: 늦게 도착) 도착 >= 체크인 :
                * 두 번째 스케줄로 "{selected_hotel_name} 체크인"을 작성 (move_time "해당 없음", 이때 basic_info에 숙소 설명 작성)

        2. 🚨 [자차 회수 절대 규칙]: 차를 특정 장소에 주차해두고 도보로 이동하다가 다시 차를 타러 갈 때는, 반드시 place_name을 "[차가 주차된 원래 장소명] (차량 회수)" 형식으로 적으세요!
           - 예시: "미담 한옥 게스트 하우스 (차량 회수)" 또는 "쌈지길 (차량 회수)"
           - 절대 장소명 없이 "차량 회수"라고만 적지 마세요! 이동 경로(route_detail)에도 [직전 장소 ➔ 도보 ➔ 차가 있는 장소]를 명확히 적으세요.
        
        3. 🚨 [일차별 마무리 및 숙소 복귀 (중복 작성 절대 금지!)]
           1. 📅 마지막 날을 제외한 모든 일차 마무리
              - 하루의 마지막 일정은 **반드시 딱 1개의 스케줄 블록**으로만 끝내야 합니다. (이동 시간 따로, 휴식 시간 따로 2개를 만들지 마세요!)
              - `place_name`: "{selected_hotel_name} 복귀 및 휴식"
              - `move_time`: 도구가 알려준 '실제 이동 시간'만 정확히 적으세요. (예: "20:30 ~ 20:45") 절대 15분 거리를 1시간으로 임의로 늘려 적지 마세요.
              - `place_time`: 숙소 도착 후 휴식 시간 (예: "20:45 ~ 22:00"). 이 시간을 활용해 밤 22시까지 남은 일정을 꽉 채워 마감하세요.
        
           2. 🏨 마지막 날 시작 (체크아웃)
              - 마지막 날 첫 번째 스케줄은 반드시 **"{selected_hotel_name} 체크아웃"**입니다.
              - `place_time`: 숙소의 체크아웃 마감 시간을 기준으로 작성하세요. (예: "10:00 ~ 11:00") **절대 비워두지 마세요.**
              - `move_time`: "해당 없음"
              
        4. 🚨 [상세 경로(route_detail) 작성 시 대중교통 역 이름 누락 절대 금지]
           - 공항, 터미널 등에서 대중교통을 탈 때, 두루뭉술하게 '공항철도 타기'라고 대충 요약하지 마세요. 도구가 알려준 정확한 지하철역 이름(예: '인천공항1터미널역')을 반드시 템플릿에 맞춰 명시해야 그래픽 UI가 깨지지 않습니다.
        
        5. 🚨 [마지막 날 귀가 스케줄 작성 (순간이동 오류 방지용 엄격 규칙)]
           마지막 관광 및 식사 일정이 끝난 후, 교통수단에 따라 귀가 스케줄을 명확히 분리해서 추가하세요.
           
           🚗 [자차 이용자 최종 귀가 (수미상관 규칙)]
           - 마지막 일정은 반드시 **"{ti.get('arrival_loc')} ➔ 서울 여행 마무리 및 자택 귀가"
           - `place_name`: "서울 여행 마무리 및 자택 귀가"
           - `move_time`: 마지막 명소에서 출발하는 시간만 작성 (예: "15:40 ~ ")
           - `place_time`: "해당 없음"
           - `route_detail` 및 `route_guide`: **"생략"** (이동 타임라인 생성 금지)
           - `guide_tip`: 👋 [가이드의 작별 인사] (여기에만 인사말을 적으세요)

           🚶 [대중교통 유저] -> 맨 마지막에 반드시 아래 '2개의 스케줄'을 순서대로 추가
           [귀가 스케줄 1 : 최종 목적지 역/터미널로 이동]
           - place_name : 무조건 최초 도착지였던 **"{ti.get('arrival_loc')}"**의 정확한 명칭을 작성하세요. (예: 서울역, 김포국제공항 등. 절대 다른 단어를 덧붙이거나 지어내지 마세요!)
           - move_time : "14:00 ~ 15:00" 처럼 역까지의 이동 시간 작성
           - route_guide, route_detail : 도구로 검색한 대중교통 상세 경로 작성
           - place_time, basic_info, guide_tip : 모두 "생략"
           
           [귀가 스케줄 2 : 최종 귀가 및 작별 인사]
           - place_name : "서울 여행 마무리 및 자택 귀가"
           - move_time : "해당 없음" (중복 출력을 막기 위해 반드시 해당 없음으로 작성)
           - route_detail, route_guide, basic_info : 모두 "생략"
           - place_time : 앞 스케줄 도착 시간을 이어받아 "15:00 ~ " 형식으로 작성
           - guide_tip : "👋 [가이드의 작별 인사] (따뜻한 멘트 2~3문장)"

        * AI 참조 날씨: {w_summary} {indoor_cmd}
        """
        dynamic_instruction = STEP_SCRIPTS["GENERATE_PLAN_WEATHER_APPLIED"] + strategic_mission

    with chat_container:
        if pre_msg:
            draw_message(pre_msg, "assistant")

        with st.spinner(spinner_msg if 'spinner_msg' in locals() else "**SeoulMate가 타이핑 중입니다.**"):
            try:        
                current_model = "⚡ Fast"
                if st.session_state.step == "GENERATE_PLAN":
                    history_len = 6 
                else:
                    if "Pro" in current_model:
                        history_len = 20  
                    elif "Deep" in current_model:
                        history_len = 15 
                    else:
                        history_len = 10
                
                trip_context = ""
                if st.session_state.trip_info:
                    ti = st.session_state.trip_info
                    trip_context = f"[여행기본정보 - {ti.get('dates', '')}({ti.get('duration', '')}일), 교통:{ti.get('transport', '')}, 도착:{ti.get('arrival_loc', '')}({ti.get('arrival_time', '')})] "

                agent_executor = get_agent_executor(current_model, st.session_state.step)
                hotel_cnt = len(st.session_state.get("shown_hotels_dict", {}))
                cache_key = f"{st.session_state.step}_{current_input}_{current_model}_{hotel_cnt}"
                
                if "response_cache" not in st.session_state: st.session_state.response_cache = {}
                force_refresh = any(kw in clean_input for kw in COMPLAINT_KWS)
                
                if not force_refresh and cache_key in st.session_state.response_cache:
                    response = st.session_state.response_cache[cache_key]
                else:
                    response = agent_executor.invoke({
                        "input": f"[현재단계: {st.session_state.step}] {trip_context}사용자입력: {current_input}{dynamic_instruction}",
                        "chat_history": st.session_state.memory.messages[-history_len:],
                        "today_date": datetime.now().strftime("%Y-%m-%d"),
                        "format_instructions": base_parser.get_format_instructions()
                    })
                    st.session_state.response_cache[cache_key] = response

                raw_output = response["output"].strip()
                json_match = re.search(r'\{.*\}', raw_output, re.DOTALL)
                if json_match:
                    raw_output = json_match.group(0)
                    
                raw_output = re.sub(r'^```json\s*|```\s*$', '', raw_output, flags=re.MULTILINE).strip()

                try:
                    try: parsed_response = base_parser.invoke(raw_output)
                    except Exception as e: parsed_response = fixing_parser.invoke(raw_output)
                    
                    if st.session_state.step == "GENERATE_PLAN" and not parsed_response.itineraries:
                        raise ValueError("AI가 일정을 짜지 않고 텍스트로만 때웠습니다.")
                    
                    chat_msg = parsed_response.chat_message.replace("\\n", "\n").strip()
                    chat_msg = re.sub(r'[\u200b\u200c\u200d\ufeff]+', '', chat_msg).strip()
                    if "말씀하신" in chat_msg and len(chat_msg) < 15:
                        chat_msg = "말씀하신 숙소의 정확한 위치를 찾을 수 없네요 😭 구, 동, 상호명을 다시 한 번 확인해 주시겠어요?"

                    if st.session_state.step == "ASK_ACCO" and (is_verifying_booking or "위치 확인 완료" in chat_msg) and parsed_response.accommodations:
                        verified_hotel = parsed_response.accommodations[0]
                        st.session_state.selected_hotel_name = verified_hotel.name
                        if "shown_hotels_dict" not in st.session_state: st.session_state.shown_hotels_dict = {}
                        h_coords = st.session_state.get("cached_places", {}).get(verified_hotel.name, {"lat": None, "lng": None})
                        st.session_state.shown_hotels_dict[verified_hotel.name] = {"name": verified_hotel.name, "check_in_out": verified_hotel.check_in_out, "lat": h_coords.get("lat"), "lon": h_coords.get("lng")}
                        selected_hotel = verified_hotel.name

                        pre_msg_veri = f"'{selected_hotel}'의 위치를 확인했습니다! 사용자님의 취향에 딱 맞는 맞춤 일정을 짜서 얼른 돌아올게요 🗓️"
                        draw_message(pre_msg_veri, "assistant")
                            
                        with st.spinner("**SeoulMate가 여행 기간의 날씨를 조회하고 있습니다.**"):
                            weather_summary, bad_weather_details = SeoulMateWeather().check_trip_weather(st.session_state.trip_info.get("dates", "2026-03-01 ~ 2026-03-03"))
                        
                        st.session_state.weather_summary = weather_summary
                        st.session_state.bad_weather_details = bad_weather_details
                        st.session_state.acco_type = "SEARCHED"
                        
                        if bad_weather_details:
                            st.session_state.step = "ASK_WEATHER_PREF"
                            dates_str = ", ".join([item[0] for item in bad_weather_details])
                            raw_reasons = []
                            for item in bad_weather_details: raw_reasons.extend(item[1].split(" 및 "))
                            reasons_str = ", ".join(list(dict.fromkeys(raw_reasons)))
                            
                            chat_msg = f"앗! 그런데 일기예보를 조회해 보니 **'{dates_str}'**에 **{reasons_str}** 예보가 있네요 🧐\n해당 날짜의 일정은 실내 동선 위주로 짜드릴까요?"
                            parsed_response.chat_message = chat_msg
                            parsed_response.accommodations = None
                            st.session_state.memory.add_ai_message(pre_msg_veri)
                            st.session_state.memory.add_ai_message(chat_msg)
                            save_current_room()
                            st.rerun()
                        else:
                            st.session_state.step = "GENERATE_PLAN"
                            st.session_state.pending_system_message = f"[시스템 긴급] 검증 완료. 날씨 이상 없음. 다음 단계를 진행하세요."
                            st.session_state.memory.add_ai_message(pre_msg_veri)
                            save_current_room()
                            st.rerun()               

                    if st.session_state.step == "ASK_ACCO":
                        if not ("cached_places" in st.session_state and len(st.session_state.cached_places) > 0):
                            parsed_response.itineraries, parsed_response.places = None, None
                    
                    if st.session_state.step == "GENERATE_PLAN" and any(kw in parsed_response.chat_message for kw in ["찾을 수 없", "빠져있네요", "확인해 주"]):
                        st.session_state.step = "ASK_ACCO" 
                        parsed_response.itineraries = None

                    if st.session_state.step == "GENERATE_PLAN":
                        parsed_response.accommodations, parsed_response.places, parsed_response.route_info = None, None, None

                    has_list = bool(parsed_response.accommodations or parsed_response.itineraries or parsed_response.places or parsed_response.route_info)
                    show_chat_msg = True
                    if has_list: show_chat_msg = False 

                    if st.session_state.step == "initial" and is_planning_intent: st.session_state.show_planner_button = True
                    elif st.session_state.step == "ASK_ACCO":
                        if parsed_response.itineraries: 
                            reset_search_context()
                            st.session_state.step = "GENERATE_PLAN"
                    elif st.session_state.step == "ASK_PLACE_PREF" and parsed_response.places:
                         reset_search_context()
                         st.session_state.step = "GENERATE_PLAN"

                    detailed_output = ""
                    
                    if parsed_response.places:
                        # 블랙리스트 관리
                        if "shown_places_dict" not in st.session_state: st.session_state.shown_places_dict = {}
                        
                        places_list = ["🗺️ 사용자님 취향 저격! 추천 장소를 찾았어요.\n"]

                        for p in parsed_response.places:
                            st.session_state.shown_places_dict[p.name] = True
                            
                            p_meta = {}
                            clean_p_name = p.name.replace(" ", "").lower()
                            
                            # 1순위: DB 상호명과 100% 완벽 일치하는 경우
                            if p.name in st.session_state.get("cached_places", {}):
                                p_meta = st.session_state.cached_places[p.name]
                            else:
                                # 2순위: 띄어쓰기나 대소문자 차이 등 최소한의 오차를 커버하기 위한 안전망
                                for c_name, c_meta in st.session_state.get("cached_places", {}).items():
                                    clean_c_name = c_name.replace(" ", "").lower()
                                    if clean_c_name in clean_p_name or clean_p_name in clean_c_name:
                                        p_meta = c_meta
                                        break
                            
                            # 별점 텍스트 완전 제거
                            item = f"📍 **{p.name}**"
                            
                            # 주소 가져오기
                            address = p_meta.get('address', '')
                            
                            if address:
                                p_raw_gd = p_meta.get("gu_dong", "")
                                p_gd_parts = [x.strip() for x in p_raw_gd.split(',')]
                                p_gd_kw = f"{p_gd_parts[0]} {p_gd_parts[1]}" if len(p_gd_parts) > 1 else p_gd_parts[0] if p_gd_parts else ""
                                
                                # 캐시된 정확한 DB 상호명을 1순위로 사용하여 지도 검색 정확도 향상
                                accurate_map_query = f"{p_meta.get('name', p.name)} {p_gd_kw}".strip()
                                enc_addr = urllib.parse.quote(accurate_map_query)
                                gmap_url = f"https://maps.google.com/maps?q={enc_addr}&t=m&z=15&output=embed"
                                google_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" style="margin-right: 3px;"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-1 .67-2.26 1.07-3.71 1.07-2.87 0-5.3-1.94-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.11c-.22-.66-.35-1.36-.35-2.11s.13-1.45.35-2.11V7.05H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.95l3.66-2.84z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.05l3.66 2.84c.86-2.59 3.29-4.53 6.16-4.53z"/></svg>'
                                gmap_btn = f"<details style='display: inline-block; margin-left: 6px; vertical-align: middle;'><summary class='gmap-btn' style='cursor: pointer; display: inline-flex; align-items: center; justify-content: center; background-color: white; border: 1px solid #dadce0; height: 18px; padding: 0 5px; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); transition: transform 0.1s ease; font-size: 11px; font-weight: 600; color: #3C4043; line-height: 1;' title='구글 지도로 보기'>{google_svg}위치 보기</summary><div style='margin-top: 10px; width: 100%; border: 1px solid #3A3A3C; border-radius: 8px; overflow: hidden; background-color: #1C1C1E;'><iframe width='100%' height='250' frameborder='0' style='border:0;' src='{gmap_url}' allowfullscreen></iframe></div></details>"
                                
                                item += f"\n🗺️ **주소** : {address}{gmap_btn}"
                                
                            item += f"\n✨ **장소 안내** : {p.reason}"
                            places_list.append(item)
                            
                        detailed_output += "\n\n".join(places_list) + "\n\n"
                        st.session_state.step = "PLACE_RECOMMENDED"

                    if parsed_response.accommodations:
                        # 예외 처리 : 기존에 보여준 숙소는 블랙리스트로 넘기고, 현재 1,2,3번은 초기화!
                        if "all_shown_hotels" not in st.session_state: 
                            st.session_state.all_shown_hotels = set()
                        if "shown_hotels_dict" in st.session_state:
                            st.session_state.all_shown_hotels.update(st.session_state.shown_hotels_dict.keys())
                        
                        st.session_state.shown_hotels_dict = {} # 1, 2, 3번 매칭을 위해 완전 초기화
                        
                        acc_list = ["🏨 짜잔! 사용자님의 조건에 딱 맞는 최적의 숙소를 찾았습니다.\n"]
                        
                        trip_info = st.session_state.get("trip_info", {})
                        is_driver = trip_info.get("transport") == "자차(직접 운전)"
                        
                        # 텍스트를 쪼개서 최대 3개까지만 보여주는 함수
                        def format_items(text, limit=3):
                            # '없음', '업체문의' 등이 텍스트 안에 있으면 아예 빈 글자를 반환해서 UI에서 숨김
                            if not text or any(kw in str(text) for kw in ['없음', '불가', '업체에 문의', '업체문의', '정보 없음', '정보없음']):
                                return ""
                            
                            items = [x.strip() for x in re.split(r'[,/]', str(text)) if x.strip()]
                            if not items: return ""
                            if len(items) > limit:
                                return ", ".join(items[:limit]) + " 등"
                            return ", ".join(items)
                        
                        for idx, acc in enumerate(parsed_response.accommodations, 1):
                            # 캐싱된 메타데이터 불러오기
                            h_meta = st.session_state.get("cached_places", {}).get(acc.name, {})
                            st.session_state.shown_hotels_dict[acc.name] = {
                                "name": acc.name, "check_in_out": acc.check_in_out, 
                                "lat": h_meta.get("lat"), "lon": h_meta.get("lng")
                            }
                            
                            contact = acc.contact if "정보 없음" not in acc.contact else ""
                            homepage = acc.homepage if "정보 없음" not in acc.homepage else ""
                            address = acc.address if "정보 없음" not in acc.address else ""
                            clean_time = acc.check_in_out.replace("체크인", "").replace("체크아웃", "").strip(" :")
                            
                            raw_rating = str(acc.rating).strip()
                            raw_review = str(acc.review_count).replace("개", "").strip()
                            disp_rating = "0" if not raw_rating or "정보" in raw_rating else raw_rating
                            disp_reviews = f"({raw_review}개)" if raw_review and "정보" not in raw_review else "(0개)"
                            raw_star = str(h_meta.get("star_rating", "")).strip()
                            if not raw_star or any(kw in raw_star for kw in ['0성급', '0', 'nan', 'None', '정보 없음', '정보없음']):
                                star_text = ""
                            else:
                                clean_star = re.sub(r'\s*(호텔).*', '', raw_star).strip()
                                star_text = f" | {clean_star}"

                            # 리스트 항목마다 네이버 예약/가격 확인 버튼 생성
                            naver_btn = ""
                            if address:
                                h_kw = h_meta.get("gu_dong", "")
                                h_parts = [p.strip() for p in h_kw.split(',')]
                                h_dong = f"{h_parts[0]} {h_parts[1]}" if len(h_parts) > 1 else h_parts[0]
                                accurate_search_name = f"{acc.name} {h_dong}".strip()
                                encoded_search_name = urllib.parse.quote(accurate_search_name)
                                h_lat = h_meta.get("lat", 37.5665)
                                h_lng = h_meta.get("lng", 126.9780)
                                n_url = f"https://map.naver.com/v5/search/{encoded_search_name}?c={h_lng},{h_lat},15,0,0,0,dh"
                                naver_btn = f"<a href='{n_url}' target='_blank' class='naver-route-btn' style='margin-left: 8px;'><span class='n-logo'>N</span><span class='n-text'>가격 및 예약 확인</span></a>"

                            # 장소명 | 성급 | 평점 | 예약버튼
                            item = f"🏠 **{acc.name}**{star_text} | ⭐ {disp_rating} {disp_reviews} {naver_btn}"
                            
                            if address:
                                map_query, encoded_query = f"{acc.name} {address}", urllib.parse.quote(f"{acc.name} {address}")
                                gmap_url = f"https://maps.google.com/maps?q={encoded_query}&t=m&z=15&output=embed"
                                google_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" style="margin-right: 3px;"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-1 .67-2.26 1.07-3.71 1.07-2.87 0-5.3-1.94-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.11c-.22-.66-.35-1.36-.35-2.11s.13-1.45.35-2.11V7.05H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.95l3.66-2.84z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.05l3.66 2.84c.86-2.59 3.29-4.53 6.16-4.53z"/></svg>'
                                gmap_html = f"<details style='display: inline-block; margin-left: 6px; vertical-align: middle;'><summary class='gmap-btn' style='cursor: pointer; display: inline-flex; align-items: center; justify-content: center; background-color: white; border: 1px solid #dadce0; height: 18px; padding: 0 5px; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); transition: transform 0.1s ease; font-size: 11px; font-weight: 600; color: #3C4043; line-height: 1;' title='구글 지도로 보기'>{google_svg}위치 보기</summary><div style='margin-top: 10px; width: 100%; border: 1px solid #3A3A3C; border-radius: 8px; overflow: hidden; background-color: #1C1C1E;'><iframe width='100%' height='250' frameborder='0' style='border:0;' src='{gmap_url}' allowfullscreen></iframe></div></details>"
                                item += f"\n🗺️ **주소** : {address}{gmap_html}"
                            
                            # 연락처
                            contact_items = []
                            if homepage: contact_items.append(f"🌐 **홈페이지** : {homepage}")
                            if contact: contact_items.append(f"📞 **전화번호** : {contact}")
                            if contact_items: item += "\n" + " | ".join(contact_items)

                            # 객실 유형 & 체크인아웃
                            item += f"\n🛏️ **객실유형** : {h_meta.get('room_type', '스탠다드')} | 🕒 **체크인 / 아웃** : {clean_time}"

                            # 부대시설 파싱
                            fac_str = format_items(h_meta.get("facilities", ""))

                            # 기타 태그 파싱
                            convenience_tags = []
                            null_kws = ["불가", "없음", "업체에 문의", "업체문의", "정보 없음", "정보없음"]

                            parking_info = str(h_meta.get("parking", "")).strip()
                            if parking_info and not any(kw in parking_info for kw in null_kws): convenience_tags.append("🅿️ 주차 가능")

                            cooking_info = str(h_meta.get("cooking", ""))
                            if cooking_info and not any(kw in cooking_info for kw in null_kws):
                                if "일부" in cooking_info: convenience_tags.append("🍳 일부만 가능")
                                else: convenience_tags.append("🍳 조리 가능")
                            
                            pickup_info = str(h_meta.get("pickup", ""))
                            if pickup_info and not any(kw in pickup_info for kw in null_kws):
                                if "부분" in pickup_info or "일부" in pickup_info: convenience_tags.append("🚐 부분 가능")
                                elif "셔틀" in pickup_info: convenience_tags.append("🚐 셔틀 운행")
                                else: convenience_tags.append("🚐 픽업 제공")
                            
                            fnb_info = str(h_meta.get("fnb", ""))
                            if fnb_info and not any(kw in fnb_info for kw in null_kws): convenience_tags.append("🍽️ 식음료장")
                            
                            if fac_str and convenience_tags: item += f"\n✨ **부대시설** : {fac_str} | 📌 **기타** : {', '.join(convenience_tags)}"
                            elif fac_str: item += f"\n✨ **부대시설** : {fac_str}"
                            elif convenience_tags: item += f"\n📌 **기타** : {', '.join(convenience_tags)}"

                            item += f"\n💡 **숙소 안내** : {acc.reason}"
                            
                            acc_list.append(item)
                        detailed_output += "\n\n".join(acc_list) + "\n\n"

                    if parsed_response.itineraries:
                        summary_output = "🎉 고민 끝에 완성된 맞춤형 일정을 확인해 보세요!\n\n\n\n"
                        
                        # 상세 일정 안내 헤더 여백 조절 및 바로 밑에 실선 구분선 추가
                        detailed_output = "<div style='font-size: 1.25em; font-weight: 800; color: #FFFFFF; margin-bottom: 12px;'>🗓️ 상세 일정 안내</div>"
                        detailed_output += "<hr style='margin: 0px; border: 0.5px solid #3A3A3C;'>\n"
                        
                        # 날씨 브리핑
                        w_summary = st.session_state.get("weather_summary", "")
                        if w_summary:
                            if parsed_response.weather_tip: 
                                summary_output += f"{w_summary}\n> 💡 **날씨 팁** : {parsed_response.weather_tip}\n<div style='margin-bottom: 25px;'></div>\n\n"
                            else: 
                                summary_output += f"{w_summary}\n<div style='margin-bottom: 25px;'></div>\n\n"

                        # 일정 요약 글자 크기 축소(1.2em) 및 하단 구분선(hr) 추가
                        summary_output += "<div style='font-size: 1.25em; font-weight: 800; color: #FFFFFF; margin-bottom: 8px;'>📋 일정 요약</div>"
                        summary_output += "<hr style='margin: 0px; border: 0.5px solid #3A3A3C;'>\n"

                        arrival_info = st.session_state.trip_info
                        start_node = {"name": arrival_info.get("arrival_loc", "출발지"), "lat": arrival_info.get("coords", (37.5665, 126.9780))[0], "lon": arrival_info.get("coords", (37.5665, 126.9780))[1]}

                        google_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" style="margin-right: 3px;"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-1 .67-2.26 1.07-3.71 1.07-2.87 0-5.3-1.94-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.11c-.22-.66-.35-1.36-.35-2.11s.13-1.45.35-2.11V7.05H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.95l3.66-2.84z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.05l3.66 2.84c.86-2.59 3.29-4.53 6.16-4.53z"/></svg>'

                        for i, day in enumerate(parsed_response.itineraries):
                            day_timeline = []
                            
                            # 일차와 일차 사이에 눈에 띄는 [점선] 구분선 삽입 (1일차는 제외)
                            if i > 0:
                                detailed_output += "<hr style='border: 0; border-top: 1.5px dashed #3A3A3C; margin: 45px 0px 30px 0px;'>\n\n"
                            
                            # 상세 말풍선 내 일차 폰트 크기 및 상단 여백 동적 조절 (구분선 아래는 여백 최소화)
                            top_margin = '0px' if i > 0 else '15px'
                            detailed_output += f"<div style='font-size: 1.1em; font-weight: 800; color: #FFFFFF; margin-top: {top_margin}; margin-bottom: 12px;'>🚩 {day.day_title}</div>\n"
                            
                            if i == 0:
                                prev_coord = start_node
                                prev_name = start_node["name"]
                                if "자차" in prev_name:
                                    target_h = st.session_state.get("selected_hotel_name", "숙소")
                                    prev_name = f"{target_h} (도착)"
                            else:
                                target_h = st.session_state.get("selected_hotel_name")
                                h_info = st.session_state.get("shown_hotels_dict", {}).get(target_h, {})
                                
                                # 2일차 시작 시 숙소 좌표 유실 방지
                                temp_c = get_place_coords(target_h)
                                if temp_c and temp_c.get("lat"):
                                    prev_coord = temp_c
                                else:
                                    prev_coord = {"name": target_h, "lat": h_info.get("lat"), "lon": h_info.get("lon")}
                                prev_name = target_h

                            for s in day.schedules:
                                clean_place_name = re.sub(r'\s*\(.*?\)', '', s.place_name).strip() 
                                clean_place_name = re.sub(r'\s*체크인.*|\s*체크아웃.*|\s*복귀.*|\s*짐\s*보관.*|\s*주차.*|\s*차량\s*회수.*', '', clean_place_name).strip()
                                
                                if not clean_place_name:
                                    clean_place_name = re.sub(r'\s*\(.*?\)', '', prev_name).strip()
                                    clean_place_name = re.sub(r'\s*체크인.*|\s*체크아웃.*|\s*복귀.*|\s*짐\s*보관.*|\s*주차.*|\s*차량\s*회수.*', '', clean_place_name).strip()

                                curr_coord = get_place_coords(clean_place_name)
                                
                                if not prev_name: prev_name = "출발지"
                                
                                clean_prev_name = re.sub(r'\s*\(.*?\)', '', prev_name)
                                clean_prev_name = re.sub(r'\s*체크인.*|\s*체크아웃.*|\s*복귀.*|\s*짐\s*보관.*|\s*주차.*|\s*차량\s*회수.*', '', clean_prev_name).strip()
                                
                                is_checkout = "체크아웃" in s.place_name
                                is_return = "복귀" in s.place_name
                                is_checkin = "체크인" in s.place_name
                                arrival_target = st.session_state.trip_info.get('arrival_loc', '')
                                is_logistics = any(kw in s.place_name for kw in ["귀가", "마무리", arrival_target, "프런트", "짐 보관", "차량 회수", "차량회수", "주차", "짐보관"]) or is_checkout or is_return

                                base_prev = re.sub(r'주차장|프런트|로비|주차|도착', '', clean_prev_name).strip()
                                base_curr = re.sub(r'주차장|프런트|로비|주차|도착', '', clean_place_name).strip()
                                
                                is_same_place = False
                                if base_prev and base_curr:
                                    is_same_place = (base_prev in base_curr) or (base_curr in base_prev)
                                
                                has_valid_move = s.move_time and "해당" not in s.move_time.replace(" ", "") and "없음" not in s.move_time.replace(" ", "") and not is_same_place
                                
                                dest_name = s.place_name if (is_return or "귀가" in s.place_name) else clean_place_name
                                p_addr = curr_coord.get("address", "") if curr_coord else ""
                                final_place_name = curr_coord.get("name", clean_place_name) if curr_coord else clean_place_name
                                display_title = s.place_name if (is_logistics or is_checkin) else final_place_name
                                is_empty_time = not s.place_time or "해당" in s.place_time.replace(" ", "") or "없음" in s.place_time.replace(" ", "") or "생략" in s.place_time.replace(" ", "")

                                # 요약 말풍선 데이터 조립
                                if has_valid_move:
                                    day_timeline.append(f"• **{s.move_time}** : {clean_prev_name} ➔ {dest_name}")
                                
                                if not is_empty_time:
                                    day_timeline.append(f"• **{s.place_time}** : {display_title}")
                                elif not is_return and not has_valid_move:
                                    day_timeline.append(f"• {display_title}")

                                # 상세 말풍선 데이터 조립
                                if has_valid_move:
                                    detailed_output += f"**{s.move_time} : {clean_prev_name} ➔ {dest_name}**\n"
                                    
                                    if s.route_guide and s.route_guide != "생략":
                                        detailed_output += f"🧭 **경로 안내** : {s.route_guide}\n"
                                    
                                    if s.route_detail and s.route_detail != "생략":
                                        nmap_mode = "transit" if any(i in s.route_detail for i in ["🚇", "🚌"]) else "car" if "🚗" in s.route_detail else "walk"
                                        
                                        if not clean_prev_name: clean_prev_name = prev_name if prev_name else "출발지"
                                        if not clean_place_name: clean_place_name = final_place_name if final_place_name else "도착지"

                                        # 네이버 지도 경로보기
                                        if prev_coord and curr_coord and prev_coord.get("lat") and prev_coord.get("lon") and curr_coord.get("lat") and curr_coord.get("lon"):
                                            s_kw = f"{clean_prev_name} {prev_coord.get('gd_kw', '')}".strip().replace(',', ' ').replace('/', ' ')
                                            e_kw = f"{clean_place_name} {curr_coord.get('gd_kw', '')}".strip().replace(',', ' ').replace('/', ' ')
                                            s_node = f"{prev_coord['lon']},{prev_coord['lat']},{urllib.parse.quote(s_kw)}"
                                            e_node = f"{curr_coord['lon']},{curr_coord['lat']},{urllib.parse.quote(e_kw)}"
                                        else:
                                            safe_prev = clean_prev_name.replace(',', ' ').replace('/', ' ')
                                            safe_curr = clean_place_name.replace(',', ' ').replace('/', ' ')
                                            s_node = f",,{urllib.parse.quote(safe_prev)}"
                                            e_node = f",,{urllib.parse.quote(safe_curr)}"
                                            
                                        naver_url = f"https://map.naver.com/p/directions/{s_node}/{e_node}/-/{nmap_mode}"
                                        naver_btn = f" <a href='{naver_url}' target='_blank' class='naver-route-btn'><span class='n-logo'>N</span><span class='n-text'>경로 보기</span></a>"
                                            
                                        safe_route_detail = s.route_detail.replace("->", "➔").replace("=>", "➔")
                                        if "\n" in safe_route_detail and "➔" not in safe_route_detail: safe_route_detail = safe_route_detail.replace("\n", " ➔ ")
                                        
                                        try:
                                            snake_ui = render_snake_route_ui(safe_route_detail, naver_btn)
                                            detailed_output += f"{snake_ui}\n<div style='margin-bottom: 10px;'></div>\n"
                                        except Exception:
                                            summary_part = safe_route_detail.split(":")[0].strip("() ") if ":" in safe_route_detail else safe_route_detail.strip()
                                            if summary_part:
                                                detailed_output += f"<div style='margin-bottom: 8px; font-size: 15px; color: #E5E5EA; display: flex; align-items: center;'>🔍 <b>상세 경로 안내</b> : {summary_part} {naver_btn}</div>\n\n"

                                is_final_return = "귀가" in s.place_name or "마무리" in s.place_name
                                is_pure_move = is_empty_time and has_valid_move and not is_final_return
                                
                                if not is_pure_move:
                                    if not is_empty_time:
                                        if is_return and has_valid_move:
                                            pass
                                        else:
                                            detailed_output += f"**{s.place_time} : {display_title}**\n"
                                    elif not is_return:
                                        detailed_output += f"**{display_title}**\n"
                                    
                                    if not is_logistics or is_checkin:
                                        if p_addr and p_addr not in ["", "정보 없음", "nan", "None"]:
                                            gd_keyword = curr_coord.get('gd_kw', '') if curr_coord else ''
                                            accurate_map_query = f"{final_place_name} {gd_keyword}".strip()
                                            enc_addr = urllib.parse.quote(accurate_map_query)
                                            gmap_url = f"https://maps.google.com/maps?q={enc_addr}&t=m&z=15&output=embed"
                                            gmap_btn = f"<details style='display: inline-block; margin-left: 6px; vertical-align: middle;'><summary class='gmap-btn' style='cursor: pointer; display: inline-flex; align-items: center; justify-content: center; background-color: white; border: 1px solid #dadce0; height: 18px; padding: 0 5px; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); font-size: 11px; font-weight: 600; color: #3C4043; line-height: 1;' title='구글 지도로 보기'>{google_svg}위치 보기</summary><div style='margin-top: 10px; width: 100%; border: 1px solid #3A3A3C; border-radius: 8px; overflow: hidden; background-color: #1C1C1E;'><iframe width='100%' height='250' frameborder='0' style='border:0;' src='{gmap_url}' allowfullscreen></iframe></div></details>"
                                            detailed_output += f"🗺️ **주소** : {p_addr} {gmap_btn}\n"
                                    
                                        if s.basic_info and s.basic_info != "생략":
                                            detailed_output += f"✨ **장소 안내** : {s.basic_info}\n"
                                
                                    if s.guide_tip and s.guide_tip != "생략":
                                        detailed_output += f"💡 **가이드 팁** : {s.guide_tip}\n\n--------\n\n"
                                    else:
                                        detailed_output += "\n--------\n\n"
                                else:
                                    detailed_output += "\n<div style='margin-bottom: 12px;'></div>\n\n"
                                
                                if not ("귀가" in s.place_name or "마무리" in s.place_name):
                                    if any(kw in s.place_name for kw in ["체크인", "체크아웃", "복귀", "짐 보관", "휴식", "프런트", "짐보관"]):
                                        target_h = st.session_state.get("selected_hotel_name")
                                        h_info = st.session_state.get("shown_hotels_dict", {}).get(target_h, {})
                                        
                                        # 짐보관/복귀 등 숙소 물릴 때 좌표 유실 방지
                                        temp_c = get_place_coords(target_h)
                                        if temp_c and temp_c.get("lat"):
                                            prev_coord = temp_c
                                        else:
                                            prev_coord = {"name": target_h, "lat": h_info.get("lat"), "lon": h_info.get("lon")}
                                            
                                        prev_name = target_h
                                    else:
                                        prev_name = final_place_name
                                        prev_coord = curr_coord

                            summary_output += f"<div style='font-size: 1.1em; font-weight: 800; color: #FFFFFF; margin-top: 15px; margin-bottom: 8px;'>🚩 {day.day_title}</div>\n"
                            summary_output += "\n".join(day_timeline) + "\n\n"

                    # 텍스트 조립 마무리
                    if parsed_response.route_info: 
                        detailed_output += f"🧭 이동 경로 안내 : \n{parsed_response.route_info}\n\n"
                    detailed_output = detailed_output.strip()
                    if 'summary_output' in locals():
                        summary_output = summary_output.strip()

                # 에러 처리
                except Exception as parse_error:
                    if st.session_state.step == "GENERATE_PLAN": error_txt = "앗! 최적의 장소와 동선을 계산하다가 오류가 발생했어요. 번거로우시겠지만 **'일정 다시 짜줘'** 라고 한 번만 더 입력해 주시겠어요? 😭"
                    else: error_txt = "앗! 결과를 정리하다가 제 머릿속이 살짝 꼬였네요. 다시 한 번만 말씀해 주시겠어요? 😅"
                    
                    st.session_state.memory.add_ai_message(error_txt)
                    save_and_embed_memory(current_input, "[파싱 에러 복구됨] " + error_txt)
                    save_current_room()
                    st.rerun()

            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "rate_limit" in error_str.lower():
                    error_txt = "앗! 일을 너무 열심히 했더니 피곤하네요. 상단에서 다른 모드 선택 후 대화를 이어가주세요 🔋"
                else: 
                    error_txt = "앗! 딴짓하느라 뭐라 말씀하셨는지 못 들었어요. 다시 한번 말씀해 주시겠어요? 🙇‍♂️"
                
                st.session_state.memory.add_ai_message(error_txt)
                save_and_embed_memory(current_input, "[런타임 에러] " + error_txt)
                save_current_room()
                st.rerun()
        
        with chat_container:
            combined_summary = ""
            
            if chat_msg and show_chat_msg:
                combined_summary += f"{chat_msg}\n\n"
                
            if 'summary_output' in locals() and summary_output:
                combined_summary += summary_output
                
            # 합쳐진 첫 번째 말풍선 렌더링
            if combined_summary.strip():
                draw_message(combined_summary.strip(), "assistant")
                st.session_state.memory.add_ai_message(combined_summary.strip())
                
            # 그다음 상세 말풍선(snake UI + 설명)을 연달아 렌더링
            if detailed_output: 
                draw_message(detailed_output, "assistant")
                st.session_state.memory.add_ai_message(detailed_output)

            post_acco_msg = ""
            post_place_msg = ""
            
            if parsed_response.accommodations:
                with st.spinner("**SeoulMate가 타이핑 중입니다.**"):
                    time.sleep(2) 
                
                post_acco_msg = "각 숙소의 **[가격 및 예약 확인]**과 **[위치 보기]** 버튼을 눌러 예약 가능 여부, 가격대, 위치를 꼼꼼히 확인해 보세요! ☺️\n\n충분히 둘러보신 후, 가장 마음에 드는 곳의 **이름 또는 순서(1번, 2번, 3번)**를 편하게 말씀해 주세요.\n혹시 다시 추천받고 싶으시다면 **'다른 곳 찾아줘'**라고 말씀해 주셔도 좋아요 😭"
                draw_message(post_acco_msg, "assistant")
                st.session_state.memory.add_ai_message(post_acco_msg)

            if parsed_response.places:
                with st.spinner("**SeoulMate가 타이핑 중입니다.**"):
                    time.sleep(2.0)

                post_place_msg = "혹시 다른 장소를 더 알아보고 싶으신가요? 아니면 사용자님의 취향을 반영한 서울 여행 일정을 짜볼까요? 🗓️"
                draw_message(post_place_msg, "assistant")
                st.session_state.memory.add_ai_message(post_place_msg)

        # 최종 상태 저장 (새로고침 시에도 동일하게 복구되도록 parts 구성)
        parts = []
        if pre_msg: parts.append(pre_msg)
        if combined_summary.strip(): parts.append(combined_summary.strip())
        if detailed_output: parts.append(detailed_output)
        if post_acco_msg: parts.append(post_acco_msg)

        save_and_embed_memory(current_input, "\n\n".join(parts).strip())
        save_current_room()
        st.rerun()

# ==============================
# 포커스 강제 이동 트리거
# ==============================
if st.session_state.get("focus_chat", False):
    st.session_state.focus_chat = False # 플래그 사용 완료 처리
    components.html(
        f"""
        <script id="focus-trigger-{uuid.uuid4()}">
        function setFocus() {{
            const chatInput = window.parent.document.querySelector('textarea[data-testid="stChatInputTextArea"]');
            if (chatInput) {{
                chatInput.focus();
            }} else {{
                window.parent.document.body.focus();
            }}
        }}
        // Streamlit 렌더링 완료 후 실행되도록 충분한 시간(500ms) 부여
        setTimeout(setFocus, 500);
        </script>
        """, height=0, width=0
    )