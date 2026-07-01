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
CSV_FILE_PATH = "서울 음식 리스트_구, 법정동 파싱까지.csv"
SAVE_FILE_NAME = "서울 음식_구글 지도 메타데이터.csv"

# ============================================================
# 수동 스텔스 모드
# ============================================================
def apply_stealth(page):
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    page.add_init_script("window.navigator.chrome = { runtime: {} };")
    page.add_init_script("Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});")
    page.add_init_script("Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});")

# ============================================================
# 이어하기용 데이터 준비 로직
# ============================================================
def get_targets():
    last_collected_name = "없음 (처음부터 시작)"
    last_name_for_logic = None
    if os.path.exists(SAVE_FILE_NAME):
        try:
            with open(SAVE_FILE_NAME, "r", encoding="utf-8-sig") as f:
                reader = list(csv.DictReader(f))
                if reader:
                    last_name_for_logic = reader[-1]["원본장소명"].strip()
                    last_collected_name = last_name_for_logic
        except: pass

    df = None
    for enc in ['utf-8-sig', 'cp949', 'euc-kr']:
        try:
            df = pd.read_csv(CSV_FILE_PATH, encoding=enc)
            break
        except: pass
    
    if df is None: return []
    
    start_index = 0
    if last_name_for_logic:
        matched_indices = df.index[df['명칭'] == last_name_for_logic].tolist()
        if matched_indices:
            start_index = matched_indices[-1] + 1
    
    start_place_name = "없음 (모든 수집 완료)"
    if start_index < len(df):
        start_place_name = df.iloc[start_index]['명칭']

    targets = []
    for _, row in df.iloc[start_index:].iterrows():
        if pd.isna(row['명칭']): continue
        name = str(row['명칭']).strip()
        
        gu = str(row.get('구명', '')).strip() if not pd.isna(row.get('구명')) else ""
        dong = str(row.get('법정동명', '')).strip() if not pd.isna(row.get('법정동명')) else ""
        
        keywords = []
        if gu: keywords.append(gu)
        if dong: keywords.append(dong)
        keywords.append(name)
        
        if not gu and not dong:
            if "서울" not in name: keywords.insert(0, "서울")
            
        search_query = " ".join(keywords)
        origin_addr = str(row.get('주소', '')).strip()
        targets.append({"name": name, "origin_addr": origin_addr, "query": search_query})
    
    print("\n" + "="*50)
    print(f"📍 마지막 수집 장소 : {last_collected_name}")
    print(f"🚀 수집 시작 장소   : {start_place_name}")
    print(f"📦 남은 수집 대상   : {len(targets)}개")
    print("="*50 + "\n")
    
    return targets

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
# 메인 실행
# ============================================================
def scrape_metadata():
    targets = get_targets()
    if not targets:
        print("🎉 모든 장소 수집이 끝났습니다!")
        return

    fieldnames = [
        "원본장소명", "원본주소", "검색어", "리뷰 수집 장소명", "리뷰 수집 주소",
        "총 평점", "리뷰 개수", "홈페이지", "전화번호", "영업시간", "휴무일"
    ]

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
                print(f"\n☕ 휴식중... ({rest_time:.1f}초 대기)")
                time.sleep(rest_time)

            place_name, origin_addr, search_query = target['name'], target['origin_addr'], target['query']
            print(f"\n[{i+1}/{len(targets)}] 🚀 수집 목표 : '{place_name}' (검색어: {search_query})")

            try:
                encoded_query = urllib.parse.quote(search_query)
                page.goto(f"https://www.google.com/maps/search/{encoded_query}")
                time.sleep(random.uniform(4.5, 5.5))

                real_google_name, real_google_address = "확인불가", "확인불가"
                h1_el = page.locator("h1.DUwDvf").first
                
                # 광고 우회 로직
                if not h1_el.is_visible():
                    results = page.locator('a.hfpxzc')
                    target_el = None
                    
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
                                target_el = link_el
                                break
                                
                    if target_el:
                        target_el.wait_for(state="visible", timeout=3000)
                        target_el.scroll_into_view_if_needed() # 스크롤까지 완벽히!
                        time.sleep(0.5)
                        print(f"   🎯 광고를 제외한 순수 검색 결과 중 첫 번째 장소 클릭")
                        human_click(page, target_el)
                        time.sleep(4.0)
                    else:
                        print("   ⚠️  검색 결과가 없거나 모두 광고입니다.")

                if h1_el.is_visible():
                    real_google_name = h1_el.inner_text().strip()
                    addr_el = page.locator('button[data-item-id="address"] div.Io6YTe').first
                    if addr_el.is_visible(): 
                        real_google_address = addr_el.inner_text().strip()
                    else:
                        addr_fallback = page.locator('div.Io6YTe').first
                        if addr_fallback.is_visible(): real_google_address = addr_fallback.inner_text().strip()

                print(f"   🔎 구글 장소명 : {real_google_name}")

                # 상세정보 패널 찾아서 스크롤 내리기
                try:
                    active_panel = page.locator('div.m6QErb').filter(has=page.locator('h1.DUwDvf')).first
                    box = active_panel.bounding_box()
                    if box: page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
                    else: page.mouse.move(200, 500)
                except:
                    page.mouse.move(200, 500)

                for _ in range(2):
                    page.mouse.wheel(0, 800)
                    time.sleep(0.5)

                # 데이터 추출 (평점, 리뷰 등)
                total_rating, review_count = "정보없음", "정보없음"
                website, phone = "정보없음", "정보없음"

                try:
                    rating_el = page.locator('div.F7nice span[aria-hidden="true"]').first
                    if rating_el.is_visible(): total_rating = rating_el.inner_text().strip()

                    review_el = page.locator('div.F7nice span[aria-label*="리뷰"]').first
                    if review_el.is_visible():
                        match = re.search(r'([\d,]+)', review_el.inner_text())
                        if match: review_count = match.group(1).replace(",", "")
                except: pass

                try:
                    web_el = page.locator('a[data-item-id="authority"] div.Io6YTe').first
                    if web_el.is_visible(): website = web_el.inner_text().strip()
                    
                    phone_el = page.locator('button[data-item-id^="phone:tel:"] div.Io6YTe').first
                    if phone_el.is_visible(): phone = phone_el.inner_text().strip()
                except: pass

                # 영업시간 & 휴무일 (특수기호 제거 및 연중무휴 처리)
                open_hours_result = "정보없음"
                closed_days_result = "정보없음"

                try:
                    btn = page.locator('[aria-label*="영업시간 표시"]').first
                    if btn.is_visible():
                        btn.click(force=True)
                        time.sleep(1.0) 

                        extracted_hours = page.evaluate('''() => {
                            let res = {};
                            let ds = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일'];
                            document.querySelectorAll('tr').forEach(tr => {
                                let text = tr.innerText;
                                ds.forEach(d => {
                                    if (text.includes(d)) {
                                        res[d] = text.replace(d, '').replace(/\\n/g, ' ').trim();
                                    }
                                });
                            });
                            return res;
                        }''')
                        
                        day_map = {"월요일": "월", "화요일": "화", "수요일": "수", "목요일": "목", "금요일": "금", "토요일": "토", "일요일": "일"}
                        STANDARD_DAYS = ['월', '화', '수', '목', '금', '토', '일']
                        
                        closed_days = []
                        hours_dict = {}

                        for full_day, time_str in extracted_hours.items():
                            if not time_str: continue
                            short_day = day_map[full_day]
                            
                            time_str = re.sub(r'[^\w\s\:\~\-\.\,\(\)가-힣]', '', time_str).strip()
                            
                            if "휴무" in time_str or "closed" in time_str.lower():
                                closed_days.append(short_day)
                            else:
                                if time_str not in hours_dict:
                                    hours_dict[time_str] = []
                                hours_dict[time_str].append(short_day)

                        closed_days.sort(key=lambda x: STANDARD_DAYS.index(x))
                        closed_days_result = ", ".join(closed_days) if closed_days else "연중무휴"
                        
                        for t_str in hours_dict:
                            hours_dict[t_str].sort(key=lambda x: STANDARD_DAYS.index(x))
                        sorted_hours_items = sorted(hours_dict.items(), key=lambda item: STANDARD_DAYS.index(item[1][0]))

                        open_parts = []
                        for t_str, d_list in sorted_hours_items:
                            days_str = ", ".join(d_list)
                            open_parts.append(f"{days_str} : {t_str}")
                        
                        open_hours_result = " / ".join(open_parts) if open_parts else "정보없음"

                except Exception as e:
                    print(f"   ⚠️ 영업시간 확인 불가")

                # 데이터 저장
                row_data = {
                    "원본장소명": place_name,
                    "원본주소": origin_addr,
                    "검색어": search_query,
                    "리뷰 수집 장소명": real_google_name,
                    "리뷰 수집 주소": real_google_address,
                    "총 평점": total_rating,
                    "리뷰 개수": review_count,
                    "홈페이지": website,
                    "전화번호": phone,
                    "영업시간": open_hours_result,
                    "휴무일": closed_days_result
                }

                with open(SAVE_FILE_NAME, "a", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writerow(row_data)

                clean_hours_log = open_hours_result[:25] + "..." if len(open_hours_result) > 25 else open_hours_result
                print(f"   ✅ [성공] 평점: {total_rating} / 웹: {website != '정보없음'} / 영업시간: {clean_hours_log}")

            except Exception as e:
                print(f"   ❌ 에러 발생: {e}")
                continue

if __name__ == "__main__":
    scrape_metadata()