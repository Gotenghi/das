import streamlit as st
import pytube
from googleapiclient.discovery import build
import pandas as pd
import re
from datetime import datetime, timedelta
import json
from collections import defaultdict
import logging
import traceback
import ssl
import certifi
import os
import openai
from dotenv import load_dotenv

# 로깅 설정
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# 환경 변수 로드 부분 수정
# load_dotenv() 제거

# API 키 체크
if not st.secrets.get("YOUTUBE_API_KEY"):
    st.error("YouTube API 키가 설정되지 않았습니다. 관리자에게 문의하세요.")
    st.stop()

if not st.secrets.get("OPENAI_API_KEY"):
    st.error("OpenAI API 키가 설정되지 않았습니다. 관리자에게 문의하세요.")
    st.stop()

# YouTube API 키 설정
YOUTUBE_API_KEY = st.secrets["YOUTUBE_API_KEY"]
youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)

# OpenAI API 키 설정
OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
openai.api_key = OPENAI_API_KEY

def parse_duration(duration):
    """YouTube API의 duration 문자열을 초 단위로 변환"""
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
    if not match:
        return 0
    
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    
    return hours * 3600 + minutes * 60 + seconds

@st.cache_data(ttl=86400)  # 24시간 캐시
def get_trending_videos():
    """인기 급상승 동영상 가져오기"""
    try:
        request = youtube.videos().list(
            part="snippet,statistics",
            chart="mostPopular",
            regionCode="KR",
            maxResults=4,
            fields="items(id,snippet(title,channelTitle,thumbnails/high/url),statistics(viewCount,commentCount))"
        )
        
        response = request.execute()
        trending_videos = []
        
        for item in response['items']:
            video_data = {
                'id': item['id'],
                'title': item['snippet']['title'],
                'thumbnail': item['snippet']['thumbnails']['high']['url'],
                'url': f"https://www.youtube.com/watch?v={item['id']}",
                'viewCount': int(item['statistics']['viewCount']),
                'commentCount': int(item['statistics'].get('commentCount', 0))
            }
            trending_videos.append(video_data)
        
        return trending_videos
        
    except Exception as e:
        logger.error(f"인기 동영상 가져오기 실패: {str(e)}")
        return []

@st.cache_data(ttl=86400)  # 24시간 캐시
def get_video_info(url):
    """YouTube 영상 정보를 가져오는 함수"""
    try:
        # URL 형식에 따라 video_id 추출
        if "youtu.be" in url:
            video_id = url.split('/')[-1]
        else:
            video_id = url.split('watch?v=')[1].split('&')[0]
        
        video_response = youtube.videos().list(
            part='snippet,statistics',
            id=video_id,
            fields='items(snippet(title,channelTitle),statistics(viewCount,likeCount,commentCount))'
        ).execute()
        
        return video_response
        
    except Exception as e:
        logger.error(f"영상 정보 가져오기 실패: {str(e)}")
        return None

@st.cache_data(ttl=3600)  # 1시간 캐시
def get_woosoo_videos():
    """웃소 채널의 최근 동영상 가져오기 (숏폼 제외)"""
    try:
        channel_id = "UCmzMtXrJgfCqA0rfhz8_P4A"
        
        request = youtube.search().list(
            part="snippet",
            channelId=channel_id,
            order="date",
            maxResults=15,
            type="video"
        )
        response = request.execute()
        
        video_ids = [item['id']['videoId'] for item in response['items']]
        
        videos_request = youtube.videos().list(
            part="snippet,statistics,contentDetails",
            id=','.join(video_ids)
        )
        videos_response = videos_request.execute()
        
        woosoo_videos = []
        for item in videos_response['items']:
            duration = item['contentDetails']['duration']
            duration_seconds = parse_duration(duration)
            
            if duration_seconds > 61:
                video_data = {
                    'id': item['id'],
                    'title': item['snippet']['title'],
                    'thumbnail': item['snippet']['thumbnails']['high']['url'],
                    'url': f"https://www.youtube.com/watch?v={item['id']}",
                    'viewCount': int(item['statistics'].get('viewCount', 0)),
                    'commentCount': int(item['statistics'].get('commentCount', 0))
                }
                woosoo_videos.append(video_data)
        
        return woosoo_videos[:4]
            
    except Exception as e:
        logger.error(f"웃소 채널 동영상 가져오기 실패: {str(e)}")
        return []

@st.cache_data(ttl=3600)  # 1시간 캐시
def get_comments(video_id):
    """YouTube 비디오의 댓글을 가져오는 함수"""
    try:
        request = youtube.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=30,
            order="relevance",
            textFormat="plainText",
            fields="items(snippet/topLevelComment/snippet(textDisplay,authorDisplayName,likeCount,publishedAt))"
        )
        
        comments_list = []
        try:
            response = request.execute()
            for item in response['items']:
                comment = item['snippet']['topLevelComment']['snippet']
                comments_list.append({
                    'text': comment['textDisplay'],
                    'authorDisplayName': comment['authorDisplayName'],
                    'likeCount': comment.get('likeCount', 0),
                    'publishedAt': comment['publishedAt']
                })
        except Exception as api_error:
            logger.warning(f"API를 통한 댓글 가져오기 실패: {str(api_error)}")
        
        return pd.DataFrame(comments_list)
        
    except Exception as e:
        logger.error(f"댓글 가져오기 실패: {str(e)}")
        return pd.DataFrame()

def parse_timestamp(text):
    """댓글에서 타임스탬프를 추출하는 함수"""
    try:
        # HH:MM:SS 또는 MM:SS 형식의 타임스탬프 찾기
        timestamp_pattern = r'(\d{1,2}:)?\d{1,2}:\d{2}'
        match = re.search(timestamp_pattern, text)
        
        if match:
            timestamp = match.group(0)
            # 시간을 초로 변환
            time_parts = timestamp.split(':')
            if len(time_parts) == 2:
                minutes, seconds = map(int, time_parts)
                total_seconds = minutes * 60 + seconds
            else:
                hours, minutes, seconds = map(int, time_parts)
                total_seconds = hours * 3600 + minutes * 60 + seconds
            return total_seconds
        return None
    except Exception as e:
        logger.error(f"타임스탬프 파싱 실패: {str(e)}")
    return None

def seconds_to_timestamp(seconds):
    """초를 타임스탬프 형식(HH:MM:SS)으로 변환하는 함수"""
    try:
        return str(timedelta(seconds=int(seconds)))
    except Exception as e:
        logger.error(f"초 변환 실패: {str(e)}")
        return "00:00:00"

def aggregate_timeline_comments(df):
    """타임스탬프별 댓글을 집계하는 함수"""
    # 타임스탬프가 있는 댓글만 필터링
    timestamp_comments = df[df['timestamp'].notna()].copy()
    
    if len(timestamp_comments) == 0:
        return {}
    
    # 비슷한 시간대(5초 이내)의 댓글을 그룹화
    timeline_data = defaultdict(lambda: {'comments': [], 'total_likes': 0, 'representative_time': 0})
    
    # 타임스탬프로 정렬
    timestamp_comments = timestamp_comments.sort_values('timestamp')
    
    current_group = None
    for _, row in timestamp_comments.iterrows():
        timestamp = row['timestamp']
        
        # 새로운 그룹 시작 또는 기존 그룹에 추가
        if current_group is None or abs(timestamp - current_group) > 5:
            current_group = timestamp
        
        timeline_data[current_group]['comments'].append(row.to_dict())
        timeline_data[current_group]['total_likes'] += row['likeCount']
        timeline_data[current_group]['representative_time'] = current_group
        
        # 각 그룹 내에서 댓글을 좋아요 순으로 정렬
        timeline_data[current_group]['comments'].sort(
            key=lambda x: x['likeCount'], 
            reverse=True
        )
    
    return timeline_data

def create_timestamp_link(url, seconds):
    """타임스탬프 링크 생성"""
    base_url = url.split('&t=')[0]  # 기존 타임스탬프 제거
    return f"{base_url}&t={int(seconds)}s"

def create_youtube_embed(url, start_time=0):
    """YouTube 임베드 iframe HTML 생성 (필요 시 사용)"""
    video_id = url.split('watch?v=')[1].split('&')[0]
    return f"""
        <iframe
            width="100%"
            height="400"
            src="https://www.youtube.com/embed/{video_id}?enablejsapi=1&start={start_time}&autoplay=1"
            frameborder="0"
            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
            allowfullscreen
        ></iframe>
    """

def format_number(num):
    """숫자를 읽기 쉬운 형식으로 변환 (예: 1000 -> 1천, 1000000 -> 100만)"""
    if num >= 100000000:  # 1억 이상
        return f"{num//100000000}억 {(num%100000000)//10000}만"
    elif num >= 10000:    # 1만 이상
        return f"{num//10000}만"
    elif num >= 1000:     # 1천 이상
        return f"{num//1000}천"
    else:
        return str(num)

def generate_comment_cards(comments):
    """댓글 카드 HTML 생성"""
    cards_html = ""
    for comment in comments:
        cards_html += f"""
            <div class="comment-card">
                <div class="comment-header">
                    <span class="comment-author">{comment['authorDisplayName']}</span>
                    <span class="comment-likes">👍 {comment['likeCount']}</span>
                </div>
                <div class="comment-text">
                    {comment['text']}
                </div>
            </div>
        """
    return cards_html

def generate_share_buttons(url, timestamp):
    """공유 버튼 HTML 생성"""
    timestamp_url = create_timestamp_link(url, timestamp)
    kakao_share_url = f"https://accounts.kakao.com/login?continue=https://sharer.kakao.com/talk/friends/picker/link?url={timestamp_url}"
    twitter_share_url = f"https://twitter.com/intent/tweet?url={timestamp_url}"
    
    return f"""
        <a href="{timestamp_url}" target="_blank" class="share-button">
            <span>🔗</span>
            <span>링크 복사</span>
        </a>
        <a href="{kakao_share_url}" target="_blank" class="share-button">
            <span>💬</span>
            <span>카카오톡 공유</span>
        </a>
        <a href="{twitter_share_url}" target="_blank" class="share-button">
            <span>🐦</span>
            <span>트위터 공유</span>
        </a>
    """

@st.cache_data(ttl=86400)  # 24시간 캐시
def generate_short_form_title(timeline_data):
    """각 타임라인 그룹별로 숏폼 제목 생성"""
    try:
        # 좋아요 순으로 상위 3개 그룹 선택
        top_moments = sorted(
            timeline_data.items(),
            key=lambda x: x[1]['total_likes'],
            reverse=True
        )[:3]
        
        titles = []
        for time, data in top_moments:
            # 해당 시간대의 모든 댓글 수집
            comments = [comment['text'] for comment in data['comments']]
            timestamp = seconds_to_timestamp(time)
            
            try:
                client = openai.OpenAI(api_key=OPENAI_API_KEY)
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": """당신은 YouTube 숏폼 콘텐츠의 제목을 생성하는 전문가입니다.
                         특히 '웃소' 채널의 특성을 잘 이해하고 있습니다:
                         - 웃소는 다양한 콘텐츠(상황극, 게임, 먹방, 브이로그)를 통해 멤버 간의 유머와 개성을 보여주는 코미디 유튜브 채널입니다.
                         - 구독자들은 웃소 멤버들의 티키타카(재치 있는 대화)와 독특한 캐릭터성을 좋아합니다.
                         - 멤버들: 해리, 태훈, 성희, 고탱, 우디, 디투, 소정 등
                         또한, 한국의 밈을 잘 이해하고 있고, 무심한 듯한 유머를 이해합니다.                   
                         """},
                        {"role": "user", "content": f"다음은 영상의 {timestamp} 부근에서 나온 시청자 댓글들입니다. "
                         f"이 댓글들 중 간결하고 임팩트 있는 댓글을 하나 뽑아 제목으로 활용해주세요. ex: 20:01 키노피오가되. > 키노피오가 되. \n\n{'\n'.join(comments)}"}
                    ],
                    temperature=0.7,
                    max_tokens=100
                )
                
                title = response.choices[0].message.content
                # 줄바꿈 문자를 공백으로 대체하여 한 줄로 만듦
                title = title.strip().replace('\n', ' ')
                titles.append(f"🎬 {timestamp} - {title}")
                
            except Exception as api_error:
                logger.error(f"OpenAI API 호출 실패: {str(api_error)}\n{traceback.format_exc()}")
                continue
        
        # 각 제목 사이에 한 번의 줄바꿈만 추가
        return "\n".join(titles) if titles else None
        
    except Exception as e:
        logger.error(f"제목 생성 실패: {str(e)}\n{traceback.format_exc()}")
        return None

def set_global_styles():
    """전역 스타일 설정"""
    st.markdown("""
        <style>
        /* 글로벌 스타일 */
        :root {
            --text-color: #1A1A1A;
            --bg-color: #FFFFFF;
            --card-bg: #F7F7F7;
            --accent-color: #FF4B4B;
            --secondary-text: #666666;
        }

        /* 다크 모드 대응 */
        @media (prefers-color-scheme: dark) {
            :root {
                --text-color: #FFFFFF;
                --bg-color: #1A1A1A;
                --card-bg: #2D2D2D;
                --secondary-text: #B0B0B0;
            }
        }

        /* 기본 텍스트 스타일 */
        .stApp {
            color: var(--text-color);
            background-color: var(--bg-color);
        }

        /* 히어로 섹션 */
        .hero-section {
            text-align: center;
            padding: 4rem 0;
            background: linear-gradient(135deg, rgba(255, 75, 75, 0.1) 0%, rgba(255, 75, 75, 0.05) 100%);
            border-radius: 24px;
            margin-bottom: 3rem;
        }

        .hero-title {
            font-size: 3.5rem;
            font-weight: 800;
            color: var(--text-color) !important;
            margin-bottom: 1rem;
        }

        .hero-subtitle {
            font-size: 2rem;
            color: var(--text-color) !important;
            margin-bottom: 1rem;
        }

        .hero-description {
            font-size: 1.2rem;
            color: var(--secondary-text) !important;
            line-height: 1.8;
        }

        /* 비디오 플레이어 */
        .video-player {
            background: var(--card-bg);
            border-radius: 16px;
            overflow: hidden;
            margin-bottom: 2rem;
        }

        .video-info-box {
            padding: 1.5rem;
        }

        .video-title {
            color: var(--text-color) !important;
            font-size: 1.2rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
        }

        .channel-name {
            color: var(--secondary-text) !important;
        }

        /* 타임라인 모먼트 */
        .moment-card {
            background: var(--card-bg);
            border-radius: 12px;
            padding: 1rem;
            margin-bottom: 1rem;
            border: 1px solid rgba(var(--text-color), 0.1);
        }

        /* 댓글 카드 */
        .comment-card {
            background: var(--card-bg);
            border-radius: 8px;
            padding: 1rem;
            margin-bottom: 0.5rem;
        }

        .comment-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 0.5rem;
        }

        .comment-author {
            color: var(--secondary-text) !important;
        }

        .comment-text {
            color: var(--text-color) !important;
        }

        /* 버튼 스타일 */
        .stButton > button {
            background-color: var(--accent-color);
            color: white;
            border: none;
            padding: 0.5rem 2rem;
            border-radius: 8px;
            font-weight: bold;
            transition: all 0.3s ease;
        }

        .stButton > button:hover {
            opacity: 0.9;
            transform: translateY(-2px);
        }

        /* 입력 필드 */
        .stTextInput > div > div {
            background-color: var(--card-bg);
            border: 1px solid rgba(var(--text-color), 0.1);
        }

        .stTextInput > div > div > input {
            color: var(--text-color);
        }

        /* AI 제안 박스 */
        .ai-suggestion-box {
            background: linear-gradient(135deg, rgba(255, 75, 75, 0.1) 0%, rgba(255, 75, 75, 0.05) 100%);
            border: 1px solid rgba(255, 75, 75, 0.2);
            border-radius: 16px;
            padding: 1.5rem;
            margin-bottom: 2rem;
        }

        .ai-suggestion-title {
            color: var(--accent-color) !important;
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 1rem;
        }

        .ai-suggestion-content {
            color: var(--text-color) !important;
            line-height: 1.6;
        }

        /* 하이라이트 */
        .highlight {
            color: var(--accent-color);
            font-weight: 600;
        }

        /* 레이아웃 최대 너비 조정 */
        .block-container {
            max-width: 95% !important;
            padding-top: 1rem !important;
            padding-right: 1rem !important;
            padding-left: 1rem !important;
        }

        /* 비디오 플레이어 크기 조정 */
        .video-player iframe {
            width: 100%;
            height: 600px;  /* 높이 증가 */
        }

        /* 타임라인 모먼트 스크롤 */
        .timeline-moments {
            max-height: calc(100vh - 100px);
            overflow-y: auto;
            padding-right: 1rem;
        }

        /* 스크롤바 스타일링 */
        .timeline-moments::-webkit-scrollbar {
            width: 6px;
        }

        .timeline-moments::-webkit-scrollbar-track {
            background: var(--card-bg);
            border-radius: 3px;
        }

        .timeline-moments::-webkit-scrollbar-thumb {
            background: var(--accent-color);
            border-radius: 3px;
        }

        /* 댓글 카드 간격 조정 */
        .comment-card {
            margin-bottom: 0.75rem;
            padding: 0.75rem;
        }

        /* 반응형 디자인 개선 */
        @media (max-width: 1200px) {
            .video-player iframe {
                height: 500px;
            }
        }

        @media (max-width: 992px) {
            .video-player iframe {
                height: 400px;
            }
        }
        </style>
    """, unsafe_allow_html=True)

def display_ai_suggestions(suggested_titles):
    """AI 제안 표시"""
    titles_list = suggested_titles.split("\n")  # 줄 바꿈을 기준으로 분리
    titles_html = "<br>".join(titles_list)  # HTML 줄 바꿈으로 변환

    st.markdown(f"""
        <div class="ai-suggestion-box">
            <h3 class="ai-suggestion-title">🤖 AI 추천 숏폼 제목</h3>
            <div class="ai-suggestion-content">{titles_html}</div>
        </div>
    """, unsafe_allow_html=True)

def display_moment(time, data):
    """타임라인 모먼트 표시"""
    col_time, col_stats = st.columns([1, 2])
    
    with col_time:
        # 키에 댓글 텍스트의 일부를 추가하여 고유성 보장
        unique_key = f"time_{time}_{hash(data['comments'][0]['text'][:20])}"
        if st.button(f"🕒 {seconds_to_timestamp(time)}", 
                    key=unique_key,
                    use_container_width=True):
            st.session_state.current_time = int(time)
            st.rerun()
    
    with col_stats:
        st.markdown(f"""
            <div class="stats">
                <span class="meta-badge">👍 {data['total_likes']}개</span>
                <span class="meta-badge">💬 {len(data['comments'])}개</span>
            </div>
        """, unsafe_allow_html=True)
    
    # 댓글 표시
    for comment in data['comments']:
        st.markdown(f"""
            <div class="comment-card">
                <div class="comment-header">
                    <span class="comment-author">{comment['authorDisplayName']}</span>
                    <span class="comment-likes">👍 {comment['likeCount']}</span>
                </div>
                <div class="comment-text">{comment['text']}</div>
            </div>
        """, unsafe_allow_html=True)

def main():
    # 테마 및 글로벌 스타일 설정
    set_global_styles()  # 스타일 설정 함수 분리
    
    # 페이지 상태 관리
    if 'page' not in st.session_state:
        st.session_state.page = 'home'
    
    # 페이지 라우팅
    if st.session_state.page == 'home':
        show_home_page()
    else:
        show_video_page()

def show_home_page():
    """홈페이지 표시"""
    # 히어로 섹션
    st.markdown("""
        <div class="hero-section">
            <div class="hero-content">
                <h1 class="hero-title">
                    💬 댓글 탐색기
                </h1>
                <h2 class="hero-subtitle">영상 최고의 순간을 찾아서</h2>
                <p class="hero-description">
                    타임라인 댓글을 인기순으로 모아보고,<br>
                    시청자들이 어떤 부분을 <span class="highlight">인상적으로 보고 있는지</span> 찾아보세요
                </p>
            </div>
        </div>
    """, unsafe_allow_html=True)
    
    # URL 입력 (엔터키 지원)
    url = st.text_input("YouTube URL을 입력하세요", key="url_input", on_change=handle_enter)
    
    if st.button("댓글 보러가기") or st.session_state.get('enter_pressed', False):
        if url:
            st.session_state.video_url = url
            st.session_state.page = 'video'
            st.session_state.enter_pressed = False
            st.rerun()
        else:
            st.error("URL을 입력해주세요")
    
    # 트렌딩 비디오 표시 (한 번만 호출)
    show_trending_videos()

def handle_enter():
    """엔터키 입력 처리"""
    st.session_state.enter_pressed = True

def show_video_page():
    """비디오 페이지 표시"""
    # 뒤로가기 버튼
    if st.button('← 홈으로', key='back_button'):
        st.session_state.page = 'home'
        st.rerun()
    
    # 비디오 처리
    if hasattr(st.session_state, 'video_url'):
        process_video(st.session_state.video_url)

def process_video(url):
    """비디오 처리 및 표시"""
    try:
        video_response = get_video_info(url)
        
        if not video_response or not video_response.get('items'):
            st.error("영상 정보를 가져올 수 없습니다.")
            return

        # 비디오 ID 추출
        video_id = url.split('watch?v=')[1].split('&')[0]
        
        # 댓글 분석
        comments_df = get_comments(video_id)
        start_time = 0
        timeline_data = {}
        current_time = st.session_state.get('current_time', 0)
        
        if not comments_df.empty:
            comments_df['timestamp'] = comments_df['text'].apply(parse_timestamp)
            timeline_data = aggregate_timeline_comments(comments_df)
            
            if timeline_data:
                most_liked_moment = max(timeline_data.items(), 
                                     key=lambda x: x[1]['total_likes'])
                start_time = most_liked_moment[0]
        
        # 레이아웃 비율 조정 (5:5로 변경)
        col1, col2 = st.columns([5, 5])
        
        with col1:
            display_video_player(video_id, video_response, current_time or start_time)
        
        with col2:
            display_timeline_moments(timeline_data, url)
                            
    except Exception as e:
        st.error(f"오류가 발생했습니다: {str(e)}")
        logger.error(f"비디오 처리 중 오류 발생: {str(e)}\n{traceback.format_exc()}")

def display_video_player(video_id, video_response, start_time):
    """비디오 플레이어 표시"""
    st.markdown(f"""
        <div class="video-player">
            <iframe
                width="100%"
                height="500"
                src="https://www.youtube.com/embed/{video_id}?start={start_time}&autoplay=1"
                frameborder="0"
                allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                allowfullscreen
            ></iframe>
            <div class="video-info-box">
                <h1 class="video-title">{video_response['items'][0]['snippet']['title']}</h1>
                <span class="channel-name">{video_response['items'][0]['snippet']['channelTitle']}</span>
            </div>
        </div>
    """, unsafe_allow_html=True)

def display_timeline_moments(timeline_data, url):
    """타임라인 모먼트 표시"""
    st.markdown("""
        <div class="timeline-moments">
            <h2>🎯 인기 타임라인 모먼트</h2>
    """, unsafe_allow_html=True)
    
    if not timeline_data:
        st.info("타임스탬프가 포함된 댓글이 없습니다.")
        return
        
    # AI 제목 생성
    suggested_titles = generate_short_form_title(timeline_data)
    if suggested_titles:
        display_ai_suggestions(suggested_titles)
    
    # 타임라인 모먼트 표시
    for time, data in sorted(timeline_data.items(), 
                          key=lambda x: x[1]['total_likes'], 
                          reverse=True)[:10]:
        display_moment(time, data)
    
    st.markdown("</div>", unsafe_allow_html=True)

def show_trending_videos():
    trending_videos = get_trending_videos()
    woosoo_videos = get_woosoo_videos()
    
    st.markdown("""
        <style>
        /* 섹션 헤더 */
        .section-header {
            position: relative;
            padding: 2rem 0 1.5rem;
            margin-bottom: 2rem;
        }
        
        .section-header::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 2px;
            background: linear-gradient(90deg, #FF4B4B 0%, rgba(255, 75, 75, 0) 100%);
        }
        
        .section-title {
            font-size: 2rem;
            font-weight: 700;
            color: white;
            margin: 0;
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        
        /* 비디오 카드 */
        .video-card {
            background: rgba(45, 45, 45, 0.5);
            border-radius: 16px;
            overflow: hidden;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border: 1px solid rgba(255, 255, 255, 0.1);
            height: 100%;
            margin-bottom: 1.5rem;
        }
        
        .video-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 12px 24px rgba(0, 0, 0, 0.3);
            border-color: rgba(255, 75, 75, 0.3);
        }
        
        .thumbnail-container {
            position: relative;
            width: 100%;
            padding-top: 56.25%;
            overflow: hidden;
        }
        
        .thumbnail-container img {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            object-fit: cover;
            transition: transform 0.3s ease;
        }
        
        .video-card:hover .thumbnail-container img {
            transform: scale(1.05);
        }
        
        .video-info {
            padding: 1.25rem;
        }
        
        .video-title {
            color: white;
            font-size: 1rem;
            font-weight: 600;
            line-height: 1.4;
            margin-bottom: 0.75rem;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            height: 2.8em;
        }
        
        .meta-row {
            display: flex;
            gap: 0.75rem;
        }
        
        .meta-badge {
            background: rgba(255, 255, 255, 0.1);
            padding: 0.4rem 0.8rem;
            border-radius: 20px;
            font-size: 0.9rem;
            color: #B0B0B0;
            display: flex;
            align-items: center;
            gap: 0.4rem;
        }
        
        /* 링크 스타일 제거 */
        a {
            text-decoration: none !important;
            color: inherit !important;
        }
        </style>
    """, unsafe_allow_html=True)
    
    # 웃소 최신 영상 섹션
    if woosoo_videos:
        st.markdown("""
            <div class="section-header">
                <h2 class="section-title">😆 웃소 최신 영상</h2>
            </div>
        """, unsafe_allow_html=True)
        
        cols = st.columns(2)
        for idx, video in enumerate(woosoo_videos[:4]):
            with cols[idx % 2]:
                st.markdown(f"""
                    <a href="{video['url']}" target="_blank">
                        <div class="video-card">
                            <div class="thumbnail-container">
                                <img src="{video['thumbnail']}" alt="{video['title']}">
                            </div>
                            <div class="video-info">
                                <h3 class="video-title">{video['title']}</h3>
                                <div class="meta-row">
                                    <span class="meta-badge">👀 {format_number(video['viewCount'])}</span>
                                    <span class="meta-badge">💬 {format_number(video['commentCount'])}</span>
                                </div>
                            </div>
                        </div>
                    </a>
                """, unsafe_allow_html=True)
    
    # 인기 급상승 동영상 섹션
    if trending_videos:
        st.markdown("""
            <div class="section-header">
                <h2 class="section-title">🔥 인기 급상승 동영상</h2>
            </div>
        """, unsafe_allow_html=True)
        
        cols = st.columns(2)
        for idx, video in enumerate(trending_videos[:4]):
            with cols[idx % 2]:
                st.markdown(f"""
                    <a href="{video['url']}" target="_blank">
                        <div class="video-card">
                            <div class="thumbnail-container">
                                <img src="{video['thumbnail']}" alt="{video['title']}">
                            </div>
                            <div class="video-info">
                                <h3 class="video-title">{video['title']}</h3>
                                <div class="meta-row">
                                    <span class="meta-badge">👀 {format_number(video['viewCount'])}</span>
                                    <span class="meta-badge">💬 {format_number(video['commentCount'])}</span>
                                </div>
                            </div>
                        </div>
                    </a>
                """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()
