import pandas as pd
import re
import time
import random
import urllib.parse
import os
import csv
from playwright.sync_api import sync_playwright

# ============================================================
# 설정
# ============================================================

CSV_FILE_PATH = "서울 숙박 리스트_최종.csv"
SAVE_FILE_NAME = "서울 숙박_구글 지도 리뷰_최종.csv"

SEOUL_GU_LIST = [
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구",
    "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구", "서초구", "성동구",
    "성북구", "송파구", "양천구", "영등포구", "용산구", "은평구", "종로구", "중구", "중랑구"
]

# ============================================================
# 수동 스텔스 모드
# ============================================================
def apply_stealth(page):
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    page.add_init_script("window.navigator.chrome = { runtime: {} };")
    page.add_init_script("Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});")
    page.add_init_script("Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});")

# ============================================================
# 이어하기 & 데이터 준비
# ============================================================
def get_collected_places():
    if not os.path.exists(SAVE_FILE_NAME): return set()
    collected = set()
    try:
        with open(SAVE_FILE_NAME, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if "원본장소명" in row and row["원본장소명"]: collected.add(row["원본장소명"].strip())
    except: pass
    print(f"✅ 이미 완료된 장소: {len(collected)}개")
    return collected

def get_targets():
    # 이미 수집된 결과 파일에서 가장 마지막 장소명 확인
    last_collected_name = "없음 (처음부터 시작)"
    last_name_for_logic = None
    if os.path.exists(SAVE_FILE_NAME):
        try:
            with open(SAVE_FILE_NAME, "r", encoding="utf-8-sig") as f:
                reader = list(csv.DictReader(f))
                if reader:
                    # 파일의 가장 마지막 행에서 장소명 추출
                    last_name_for_logic = reader[-1]["원본장소명"].strip()
                    last_collected_name = last_name_for_logic
        except: pass

    # 원본 데이터 로드
    df = None
    for enc in ['utf-8-sig', 'cp949', 'euc-kr']:
        try:
            df = pd.read_csv(CSV_FILE_PATH, encoding=enc)
            break
        except: pass
    
    if df is None: return []
    
    # 마지막 장소의 인덱스 찾기 및 시작 지점 설정
    start_index = 0
    if last_name_for_logic:
        matched_indices = df.index[df['명칭'] == last_name_for_logic].tolist()
        if matched_indices:
            # 마지막 성공한 장소의 바로 다음 인덱스부터 시작
            start_index = matched_indices[-1] + 1
    
    # 수집 시작 장소명 확인
    start_place_name = "없음 (모든 수집 완료)"
    if start_index < len(df):
        start_place_name = df.iloc[start_index]['명칭']

    # 대상 리스트 생성 (start_index부터 끝까지 슬라이싱)
    targets = []
    for _, row in df.iloc[start_index:].iterrows():
        if pd.isna(row['명칭']): continue
        name = str(row['명칭']).strip()
        
        gu = str(row.get('구명', '')).strip() if not pd.isna(row.get('구명')) else ""
        dong = str(row.get('법정동명', '')).strip() if not pd.isna(row.get('법정동명')) else ""
        
        # 검색어 조합(구 + 법정동 + 장소명)
        keywords = []
        if gu: keywords.append(gu)
        if dong: keywords.append(dong)
        keywords.append(name)
        
        # 주소 정보가 아예 없는 예외 케이스 처리(장소명에 서울이 없는 경우 서울 삽입)
        if not gu and not dong:
            if "서울" not in name: keywords.insert(0, "서울")
            
        search_query = " ".join(keywords)
            
        origin_addr = str(row.get('주소', '')).strip()
        targets.append({"name": name, "origin_addr": origin_addr, "query": search_query})
    
    # 로그 출력 형식 최적화
    print("\n" + "="*50)
    print(f"📍 마지막 수집 장소 : {last_collected_name}")
    print(f"🚀 수집 시작 장소   : {start_place_name}")
    print(f"📦 남은 수집 대상   : {len(targets)}개")
    print("="*50 + "\n")
    
    return targets

# ============================================================
# 물리적 클릭
# ============================================================
def human_click(page, locator):
    try:
        box = locator.bounding_box()
        if box:
            x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
            page.mouse.move(x, y, steps=10)
            time.sleep(random.uniform(0.1, 0.3))
            page.mouse.down()
            time.sleep(random.uniform(0.05, 0.15))
            page.mouse.up()
            return True
    except: pass
    return False

# ============================================================
# 킥스타트 스크롤 함수 (중복 제거용)
# ============================================================
def kickstart_scroll(page):
    """리뷰 패널을 찾아 내렸다가 다시 올려서 데이터를 로딩시킴"""
    print("      ⬇️  리뷰 로딩을 위해 강제 스크롤 시도...")
    scrollable_panel_selector = 'div.m6QErb[jslog*="26354"]'
    
    try:
        try:
            page.wait_for_selector(scrollable_panel_selector, state="visible", timeout=5000)
        except: pass
        
        panel = page.locator(scrollable_panel_selector).first
        box = panel.bounding_box()
        
        if box:
            center_x = box['x'] + box['width'] / 2
            center_y = box['y'] + box['height'] / 2
            page.mouse.move(center_x, center_y)
        else:
            page.mouse.move(200, 500)
    except:
        page.mouse.move(200, 500)

    page.mouse.wheel(0, 4000) 
    time.sleep(2.0)
    
    print("      ⬆️  처음부터 수집하기 위해 스크롤 원위치...")
    page.mouse.wheel(0, -4000)
    time.sleep(1.0)

# ============================================================
# 메인 실행 (탭 클릭 -> 단순 이동 -> 휠 스크롤)
# ============================================================
def scrape_with_verification():
    targets = get_targets()
    if not targets:
        print("🎉 모든 장소 수집이 끝났습니다!")
        return

    fieldnames = ["원본장소명", "원본주소", "검색어", "리뷰 수집 장소명", "리뷰 수집 주소", "정렬기준", "별점", "날짜", "내용"]

    if not os.path.exists(SAVE_FILE_NAME):
        with open(SAVE_FILE_NAME, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

    with sync_playwright() as p:
        user_data_dir = os.path.join(os.getcwd(), "chrome_dummy_profile")
        browser = p.chromium.launch_persistent_context(
            user_data_dir, headless=False, channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
            ignore_default_args=["--enable-automation"],
            locale="ko-KR", timezone_id="Asia/Seoul",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = browser.pages[0]
        browser.on("page", lambda p: p.close())
        apply_stealth(page)

        print("\n🔑 구글 로그인 페이지 이동 (30초 대기)...")
        page.goto("https://accounts.google.com")
        for i in range(30, 0, -1):
            print(f"남은 시간: {i}초...", end='\r')
            time.sleep(1)
        print("\n🚀 수집 시작!")

        for i, target in enumerate(targets):
            if i > 0 and i % 300 == 0:
                rest_time = random.uniform(30, 60)
                print(f"\n☕ 잠시 휴식중... ({rest_time:.1f}초 대기)")
                time.sleep(rest_time)

            place_name, origin_addr, search_query = target['name'], target['origin_addr'], target['query']
            print(f"\n[{i+1}/{len(targets)}] 🚀 수집 목표 : '{place_name}' (검색어: {search_query})")

            unique_reviews = set() 
            local_data = []
            try:
                encoded_query = urllib.parse.quote(search_query)
                page.goto(f"https://www.google.com/maps/search/{encoded_query}")
                time.sleep(random.uniform(4.5, 5.5))

                real_google_name, real_google_address = "확인불가", "확인불가"
                h1_el = page.locator("h1.DUwDvf").first
                
                # 광고 우회 로직
                if not h1_el.is_visible():
                    results = page.locator('a.hfpxzc')
                    target = None
                    
                    if results.count() > 0:
                        ad_keywords = ["광고", "Sponsored", "Ad", "스폰서"]
                        
                        for idx in range(results.count()):
                            link_el = results.nth(idx)
                            parent_card = link_el.locator("xpath=..")
                            
                            is_ad = False
                            for kw in ad_keywords:
                                if parent_card.get_by_text(kw, exact=True).count() > 0:
                                    is_ad = True
                                    break
                            
                            if not is_ad:
                                target = link_el
                                break
                                
                    if target:
                        target.wait_for(state="visible", timeout=3000)
                        target.scroll_into_view_if_needed()
                        time.sleep(0.5)
                        print(f"   🎯 광고를 제외한 순수 검색 결과 중 첫 번째 장소 클릭")
                        human_click(page, target)
                        time.sleep(4.0)
                    else:
                        print("   ⚠️  검색 결과가 없거나 모두 광고입니다.")
                
                if h1_el.is_visible():
                    real_google_name = h1_el.inner_text().strip()
                    addr_el = page.locator('div.Io6YTe').first
                    if addr_el.count() > 0: real_google_address = addr_el.inner_text().strip()

                print(f"   🔎 수집된 장소명 : {real_google_name}")
                print(f"   🏠 수집된 주소 : {real_google_address}")

                # 스크롤 로직 : 클릭 -> 단순 이동 -> 휠
                review_tab = page.locator('button[role="tab"][aria-label*="리뷰"], button:has-text("리뷰")').first
                
                try:
                    review_tab.wait_for(state="visible", timeout=3000)
                    review_tab.click(force=True)
                    time.sleep(2.0) 

                    # [최적화] 조건부 킥스타트 : 리뷰가 바로 보이면 스크롤 생략
                    print("      👀 리뷰가 바로 보이는지 확인 중...")
                    need_kickstart = False
                    
                    try:
                        page.wait_for_selector("div.jftiEf", state="visible", timeout=3000)
                        print("      ✅ 리뷰 확인 (킥스타트 생략)")
                    except:
                        print("      ⚠️  리뷰 확인 불가 (킥스타트 실행)")
                        need_kickstart = True

                    if need_kickstart:
                        kickstart_scroll(page)
                        try:
                            page.wait_for_selector("div.jftiEf", state="visible", timeout=5000)
                        except:
                            pass

                    if page.locator("div.jftiEf").count() == 0:
                        print("   ℹ️  리뷰 탭이 없습니다.")
                        continue
                    
                except Exception as e:
                    print(f"   ⚠️  상단 리뷰 탭 진입 중 문제 발생: {e}")
                    continue

                sort_configs = [
                    {"name": "관련성순", "keyword": "관련성"},
                    {"name": "최신순", "keyword": "최신"},
                    {"name": "높은 평점순", "keyword": "높은"},
                    {"name": "낮은 평점순", "keyword": "낮은"}
                ]
                
                for config in sort_configs:
                    sort_name, keyword = config["name"], config["keyword"]
                    print(f"      👉 [{sort_name}] 정렬 및 수집 중...")
                    sort_count = 0
                    
                    if sort_name != "관련성순":
                        try:
                            page.mouse.wheel(0, -10000)
                            time.sleep(0.8)

                            container = page.locator('div.m6QErb[jslog*="26354"]').first
                            sort_btn = container.locator('button').filter(has_text=re.compile(r"정렬|순")).filter(visible=True).first
                            
                            sort_btn.click(force=True)
                            page.locator('div[role="menu"]').wait_for(state="visible", timeout=3000)
                            
                            option = page.locator('div[role="menuitemradio"], div[role="menuitem"]').filter(has_text=keyword).first
                            
                            if option.is_visible():
                                page.evaluate('() => { document.querySelectorAll("div.jftiEf").forEach(el => el.remove()); }')
                                option.click(force=True)
                                time.sleep(2.0)

                                # [최적화] 정렬 후 조건부 킥스타트
                                need_kickstart_sort = False
                                try:
                                    page.wait_for_selector("div.jftiEf", state="visible", timeout=3000)
                                    print("      ✅ 정렬 후 리뷰 확인 (킥스타트 생략)")
                                except:
                                    print("      ⚠️  정렬 후 리뷰 확인 불가 (킥스타트 실행)")
                                    need_kickstart_sort = True
                                
                                if need_kickstart_sort:
                                    kickstart_scroll(page)
                                    try:
                                        page.wait_for_selector("div.jftiEf", state="visible", timeout=6000)
                                    except:
                                        time.sleep(2.0)
                            else: continue
                        except Exception as e:
                            print(f"      ⚠️  정렬 전환 오류: {sort_name}")
                            continue

                    try:
                        container = page.locator('div.m6QErb[jslog*="26354"]').first
                        box = container.bounding_box()
                        if box:
                            page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
                        else:
                            page.mouse.move(200, 500)
                    except:
                        page.mouse.move(200, 500)

                    no_change_count = 0

                    for scroll_step in range(5): 
                        before_count = page.locator("div.jftiEf").count()
                        page.mouse.wheel(0, 8000)
                        try:
                            page.wait_for_function(
                                f"document.querySelectorAll('div.jftiEf').length > {before_count}",
                                timeout=4000
                            )
                        except: pass 
                        time.sleep(1)

                        for attempt in range(3):
                            try:
                                btns = page.locator('button:has-text("자세히")').all()
                                if not btns: break 

                                for btn in btns:
                                    if btn.is_visible(): 
                                        btn.evaluate("b => b.click()")
                                        time.sleep(0.1)
                                break 
                            except:
                                time.sleep(0.5)
                                continue

                        current_count = page.locator("div.jftiEf").count()
                        if current_count <= before_count:
                            no_change_count += 1
                            if no_change_count >= 3: break 
                        else:
                            no_change_count = 0

                    reviews = page.locator("div.jftiEf").all()
                    for rev in reviews:
                        try:
                            # 사장님 답글 제외 (div.MyEned 안의 텍스트만)
                            text_el = rev.locator("div.MyEned .wiI7pd").first
                            if text_el.count() == 0: continue
                            text = text_el.inner_text().replace("\n", " ")
                            
                            stars_raw = ""
                            tour_s = rev.locator(".kvMYJc").first
                            hotel_s = rev.locator(".fzvQIb").first
                            if tour_s.count() > 0: stars_raw = tour_s.get_attribute("aria-label")
                            elif hotel_s.count() > 0: stars_raw = hotel_s.inner_text()
                            
                            stars_clean = "별점없음"
                            star_match = re.search(r"(\d+\.?\d*)", stars_raw.split('/')[0])
                            if star_match: stars_clean = star_match.group(1)

                            date = "날짜미상"
                            tour_d = rev.locator(".rsqaWe").first
                            hotel_d = rev.locator(".xRkPPb").first
                            if tour_d.count() > 0: date = tour_d.inner_text()
                            elif hotel_d.count() > 0:
                                d_txt = hotel_d.inner_text()
                                date = d_txt.split("Google")[0].strip()
                            
                            review_key = (text, stars_clean, date)
                            if review_key in unique_reviews: continue
                            unique_reviews.add(review_key)
                            
                            local_data.append({
                                "원본장소명": place_name, "원본주소": origin_addr, "검색어": search_query,
                                "리뷰 수집 장소명": real_google_name, "리뷰 수집 주소": real_google_address,
                                "정렬기준": sort_name, "별점": stars_clean, "날짜": date, "내용": text
                            })
                            sort_count += 1
                        except: continue 
                    print(f"      [{sort_name}] 수집 완료 ({sort_count}건)")
                
                print(f"   ✨ 총 {len(local_data)}건 수집")
                
            except Exception as e:
                print(f"   ❌ 에러 발생: {e}")
                continue

            if local_data:
                with open(SAVE_FILE_NAME, "a", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writerows(local_data)

if __name__ == "__main__":
    scrape_with_verification()