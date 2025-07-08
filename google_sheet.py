import asyncio
import nest_asyncio
import json
import os
import platform
import uuid
import time
from typing import List, Optional, Dict, Any, AsyncGenerator
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages.ai import AIMessageChunk
from langchain_core.messages.tool import ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.runnables import RunnableConfig

from fastapi import FastAPI, HTTPException, Request, Header
from typing import Optional

# 환경 변수 설정
load_dotenv(override=True)

# Pydantic 모델 정의 (OpenAI API 호환)
class ChatMessage(BaseModel):
    role: str = Field(..., description="메시지 역할 (system, user, assistant)")
    content: str = Field(..., description="메시지 내용")

class ChatCompletionRequest(BaseModel):
    model: str = Field(default="gpt-4o-mini", description="사용할 모델")
    messages: List[ChatMessage] = Field(..., description="대화 메시지들")
    stream: bool = Field(default=False, description="스트리밍 응답 여부")
    temperature: float = Field(default=0.1, ge=0, le=2, description="응답 창의성")
    max_tokens: Optional[int] = Field(default=16000, description="최대 토큰 수")

class ChatCompletionChoice(BaseModel):
    index: int
    message: Optional[ChatMessage] = None
    delta: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None

class ChatCompletionResponse(BaseModel):
    id: str
    object: str
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Optional[Dict[str, int]] = None

SYSTEM_PROMPT = """
<ROLE>
당신은 고객과 전화 통화하듯 친절하고 정중하게 대화하는 헤어샵 예약 담당자입니다.
질문을 받으면 가장 적절한 도구를 사용해 답변하세요.
답변하지 못하면 다른 도구를 시도해 더 많은 문맥을 찾아 해결하세요.
전화 통화하듯 구어체로 공손하게 답하세요.
</ROLE>

----

<INSTRUCTIONS>
Step 1: 질문 분석
- 사용자의 질문과 최종 목적을 파악하세요.
- 질문이 여러 부분으로 되어 있으면 작은 단위로 나눠 해결하세요.

Step 2: 도구 선택
- 질문을 해결할 가장 적절한 도구를 선택하세요.
- 실패 시 다른 도구를 시도해보세요.

Step 3: 답변
- 고객이 사용한 언어로, 전화 통화하듯 구어체로 공손하게 답하세요.

Step 4: 출처 제공
- 도구를 사용했다면 출처(웹 URL이나 문서)를 반드시 포함하세요.

Guidelines:
- 답변은 질문과 같은 언어로 작성하세요.
- 간결하면서도 핵심적인 답변을 주세요.
- 답변에는 다른 정보(코멘트 등)를 넣지 마세요.
- 예약 날짜는 항상 2025-MM-DD 형식으로 작성하세요.
</INSTRUCTIONS>

----

<PROCESS>
A. 예약하기
    - 문서 ID: "1lXs3JrOuvBSew2EJUZhEeaEQfGaSqIcuKcVicOkRxMQ"
    - 시트 이름: "시트1"
    - 필수 정보: 성명, 예약일, 예약시간, 시술 종류
    - 정보 부족 시 정중히 요청하세요.
    - 필요한 정보가 다 모이면 get_sheet_data로 기존 예약을 확인하세요.
    - 같은 날짜+시간에 예약이 있으면 예약하지 마세요.
        → "해당 시간에는 이미 예약이 있습니다. 다른 시간대를 선택해주세요."
        → 다른 시간대 2~3개 추천
    - 중복 없으면 빈 행 중 최상단에 update_cells을 활용하여 새 예약을 입력하세요.

B. 예약 취소하기
    - 같은 문서를 사용합니다.
    - get_sheet_data로 이름을 찾아 해당 행을 비우세요.
    - 아래 행들을 한 줄씩 위로 올리세요.
</PROCESS>
"""

class HospitalReservationAgent:
    def __init__(self):
        self.agent = None
        self.client = None
        self.sessions = {}  # 세션별 설정 저장
        
    async def initialize(self):
        """에이전트 초기화"""
        try:
            print("🔄 에이전트 초기화 시작...")
            
            # config.json 파일 로드
            with open("config.json", 'r', encoding='utf-8') as f:
                mcp_config = json.load(f)
                print(f"✅ config.json 로드 완료")
            
            # MCP 클라이언트 초기화
            self.client = MultiServerMCPClient(mcp_config)
            await self.client.__aenter__()
            tools = self.client.get_tools()
            
            print(f"🔧 도구 로드 완료: {len(tools)}개")
            
            # OpenAI 모델 사용
            model = ChatOpenAI(
                model='gpt-4o-mini',
                temperature=0.1,
                max_tokens=16000,
            )
            print("🤖 OpenAI 모델 초기화 완료")
            
            # 에이전트 생성
            self.agent = create_react_agent(
                model,
                tools,
                checkpointer=MemorySaver(),
                prompt=SYSTEM_PROMPT,
            )
            print("🎯 에이전트 생성 완료")
            
        except Exception as e:
            print(f"❌ 에이전트 초기화 실패: {str(e)}")
            raise
    
    async def cleanup(self):
        """리소스 정리"""
        if self.client:
            try:
                await self.client.__aexit__(None, None, None)
                print("🧹 MCP 클라이언트 정리 완료")
            except Exception as e:
                print(f"⚠️ 클라이언트 정리 중 오류: {e}")
    
    def get_session_config(self, session_id: str) -> RunnableConfig:
        """세션별 설정 반환"""
        if session_id not in self.sessions:
            self.sessions[session_id] = RunnableConfig(
                configurable={"thread_id": session_id}
            )
        return self.sessions[session_id]
    
    async def stream_chat(self, messages: List[ChatMessage], session_id: str) -> AsyncGenerator[str, None]:
        """스트리밍 방식으로 대화하기"""
        if not self.agent:
            raise Exception("에이전트가 초기화되지 않았습니다.")
        
        try:
            # 전체 대화 히스토리를 LangChain 메시지로 변환
            langchain_messages = []
            langchain_messages.append(HumanMessage(content=msg.content))

            # for msg in messages:
            #     if msg.role == "user":
            #         langchain_messages.append(HumanMessage(content=msg.content))
            #     elif msg.role == "assistant":
            #         from langchain_core.messages import AIMessage
            #         langchain_messages.append(AIMessage(content=msg.content))
            #     elif msg.role == "system":
            #         from langchain_core.messages import SystemMessage
            #         langchain_messages.append(SystemMessage(content=msg.content))
            
            # 메시지가 없으면 에러
            if not langchain_messages:
                yield "메시지를 찾을 수 없습니다."
                return
            config = self.get_session_config(session_id)
            
            # 전체 메시지 히스토리 전달
            async for chunk in self.agent.astream(
                {"messages": langchain_messages},  
                stream_mode="messages",
                config=config
            ):
                if isinstance(chunk[0], ToolMessage):
                    yield "요청을 처리 중입니다. 잠시만 기다려주세요."
                if chunk[0].additional_kwargs:
                    pass
                else:
                    token = chunk[0].content
                    yield token
                # # 에이전트 메시지 처리
                # if 'tool_calls' in chunk[0].additional_kwargs:
                #     print()
                # else:
                #     token = chunk[0].content
                #     yield token
                
        except Exception as e:
            error_msg = f"대화 중 오류가 발생했습니다: {str(e)}"
            yield error_msg

# 전역 에이전트 인스턴스
agent_instance = None

async def get_agent():
    """에이전트 인스턴스 반환"""
    global agent_instance
    if agent_instance is None:
        agent_instance = HospitalReservationAgent()
        await agent_instance.initialize()
    return agent_instance

# FastAPI 애플리케이션 생성
app = FastAPI(
    title="병원 예약 에이전트 API",
    description="Google Sheets를 활용한 병원 예약 관리 시스템",
    version="1.0.0"
)

@app.on_event("startup")
async def startup_event():
    """서버 시작 시 에이전트 초기화"""
    print("🚀 서버 시작 중...")
    
    # 운영 체제별 이벤트 루프 정책 설정
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    # nest_asyncio 적용
    try:
        nest_asyncio.apply()
        print("🔄 nest_asyncio 적용 완료")
    except:
        print("ℹ️ nest_asyncio 적용 건너뜀")
    
    # 에이전트 초기화
    await get_agent()
    print("✅ 서버 시작 완료!")

@app.on_event("shutdown")
async def shutdown_event():
    """서버 종료 시 리소스 정리"""
    global agent_instance
    if agent_instance:
        await agent_instance.cleanup()
    print("👋 서버 종료 완료")

@app.get("/")
async def root():
    """루트 엔드포인트"""
    return {
        "message": "병원 예약 에이전트 API",
        "version": "1.0.0",
        "endpoints": {
            "chat": "/v1/chat/completions",
            "health": "/health"
        }
    }

@app.get("/health")
async def health_check():
    """헬스 체크 엔드포인트"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    x_session_id: Optional[str] = Header(None)  # 헤더에서 세션 ID 받기
):
    """OpenAI 호환 채팅 완료 엔드포인트"""
    try:
        agent = await get_agent()
        
        # 세션 ID 생성 (실제 구현에서는 요청에서 추출하거나 사용자 인증을 통해 설정)
        session_id = x_session_id or "default_session"
        
        # 스트리밍 응답
        if request.stream:
            async def generate_stream():
                response_id = f"chatcmpl-{uuid.uuid4().hex}"
                created = int(time.time())
                
                # 스트림 시작
                yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': request.model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"
                
                # 에이전트 응답 스트리밍
                async for content_chunk in agent.stream_chat(request.messages, session_id):
                    if content_chunk.strip():  # 빈 내용 제외
                        chunk_data = {
                            'id': response_id,
                            'object': 'chat.completion.chunk',
                            'created': created,
                            'model': request.model,
                            'choices': [{
                                'index': 0,
                                'delta': {'content': content_chunk},
                                'finish_reason': None
                            }]
                        }
                        yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
                
                # 스트림 종료
                end_chunk = {
                    'id': response_id,
                    'object': 'chat.completion.chunk',
                    'created': created,
                    'model': request.model,
                    'choices': [{
                        'index': 0,
                        'delta': {},
                        'finish_reason': 'stop'
                    }]
                }
                yield f"data: {json.dumps(end_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            
            return StreamingResponse(
                generate_stream(),
                media_type="text/plain",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Content-Type": "text/plain; charset=utf-8"
                }
            )
        
        # 일반 응답
        else:
            full_response = ""
            async for content_chunk in agent.stream_chat(request.messages, session_id):
                full_response += content_chunk
            
            response = ChatCompletionResponse(
                id=f"chatcmpl-{uuid.uuid4().hex}",
                object="chat.completion",
                created=int(time.time()),
                model=request.model,
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        message=ChatMessage(role="assistant", content=full_response),
                        finish_reason="stop"
                    )
                ],
                usage={
                    "prompt_tokens": len(str(request.messages)),
                    "completion_tokens": len(full_response),
                    "total_tokens": len(str(request.messages)) + len(full_response)
                }
            )
            
            return response
            
    except Exception as e:
        print(f"❌ API 오류: {str(e)}")
        raise HTTPException(status_code=500, detail=f"내부 서버 오류: {str(e)}")

# 기본 채팅 엔드포인트 (간단한 테스트용)
@app.post("/chat")
async def simple_chat(message: dict):
    """간단한 채팅 엔드포인트"""
    try:
        agent = await get_agent()
        session_id = str(uuid.uuid4())
        
        user_message = message.get("message", "")
        if not user_message:
            raise HTTPException(status_code=400, detail="메시지가 필요합니다.")
        
        messages = [ChatMessage(role="user", content=user_message)]
        
        response = ""
        async for chunk in agent.stream_chat(messages, session_id):
            response += chunk
        
        return {"response": response}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    print("🏥 병원 예약 에이전트 FastAPI 서버")
    print("=" * 50)
    
    # 서버 실행
    uvicorn.run(
        "api_google_sheet:app",  # 실제 파일명에 맞게 수정하세요
        host="0.0.0.0",
        port=6003,
        reload=True,
        log_level="info"
    )


# import asyncio
# import nest_asyncio
# import json
# import os
# import platform
# import uuid

# from langgraph.prebuilt import create_react_agent
# from langchain_openai import ChatOpenAI
# from langchain_core.messages import HumanMessage
# from dotenv import load_dotenv
# from langchain_mcp_adapters.client import MultiServerMCPClient
# from langchain_core.messages.ai import AIMessageChunk
# from langchain_core.messages.tool import ToolMessage
# from langgraph.checkpoint.memory import MemorySaver
# from langchain_core.runnables import RunnableConfig

# os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = "/home/ubuntu/Desktop/mcp_sheets.json"

# # 환경 변수 로드 (.env 파일에서 API 키 등의 설정을 가져옴)
# load_dotenv(override=True)

# # config.json 파일 경로 설정
# CONFIG_FILE_PATH = "config.json"

# # JSON 설정 파일 로드 함수
# def load_config_from_json():
#     """
#     config.json 파일에서 설정을 로드합니다.
#     파일이 없는 경우 기본 설정으로 파일을 생성합니다.

#     반환값:
#         dict: 로드된 설정
#     """
#     default_config = {
#         "get_current_time": {
#             "command": "python",
#             "args": ["./mcp_server_time.py"],
#             "transport": "stdio"
#         }
#     }
    
#     try:
#         if os.path.exists(CONFIG_FILE_PATH):
#             with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
#                 return json.load(f)
#         else:
#             # 파일이 없는 경우 기본 설정으로 파일 생성
#             save_config_to_json(default_config)
#             return default_config
#     except Exception as e:
#         print(f"설정 파일 로드 중 오류 발생: {str(e)}")
#         return default_config

# # JSON 설정 파일 저장 함수
# def save_config_to_json(config):
#     """
#     설정을 config.json 파일에 저장합니다.

#     매개변수:
#         config (dict): 저장할 설정
    
#     반환값:
#         bool: 저장 성공 여부
#     """
#     try:
#         with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
#             json.dump(config, f, indent=2, ensure_ascii=False)
#         return True
#     except Exception as e:
#         print(f"설정 파일 저장 중 오류 발생: {str(e)}")
#         return False


# SYSTEM_PROMPT = """<ROLE>
# You are hair shop reservation agent with an ability to use tools. 
# You will be given a question and you will use the tools to answer the question.
# Pick the most relevant tool to answer the question. 
# If you are failed to answer the question, try different tools to get context.
# Answer as if you’re talking to a customer on the phone — keep it polite and conversational.
# </ROLE>

# ----

# <INSTRUCTIONS>
# Step 1: Analyze the question
# - Analyze user's question and final goal.
# - If the user's question is consist of multiple sub-questions, split them into smaller sub-questions.

# Step 2: Pick the most relevant tool
# - Pick the most relevant tool to answer the question.
# - If you are failed to answer the question, try different tools to get context.

# Step 3: Answer the question
# - Answer the question in the same language as the question.
# - Answer as if you’re talking to a customer on the phone — keep it polite and conversational.

# Step 4: Provide the source of the answer(if applicable)
# - If you've used the tool, provide the source of the answer.
# - Valid sources are either a website(URL) or a document(PDF, etc).

# Guidelines:
# - Answer in the same language as the question.
# - Answer should be concise and to the point.
# - Avoid response your output with any other information than the answer and the source.  
# - Use the format 2025-MM-DD to record the reservation date. This year is 2025. Do not change it.
# </INSTRUCTIONS>

# <PROCESS>
# A. 예약하기
#     1. 문서ID는 "1lXs3JrOuvBSew2EJUZhEeaEQfGaSqIcuKcVicOkRxMQ" 시트의 이름은 "시트1"입니다.
#     2. 성명, 예약일, 예약시간, 시술 종류는 필수요소입니다. 정보가 부족하다면 정중하게 요청하세요.
#     3. 필요한 정보가 다 수집되었다면 get_sheet_data 툴을 활용하여 기존 예약 목록을 확인하세요.
#     4. 예약일과 예약시간이 모두 동일한 정보가 존재한다면, 예약을 절대 진행하지 마세요.
#        - 중복 예약이 감지되었을 경우 반드시 다음과 같이 응답하세요:
#          - "해당 시간에는 이미 예약이 있습니다. 다른 시간대를 선택해주세요."
#        - 가능한 다른 시간대를 2~3개 추천하세요.
#     5. 중복 예약이 없음을 확인한 후, 빈 행을 탐색하여 해당 위치에 정보를 기입하세요.
#     6. update_cells 툴을 활용하여 새로운 예약정보를 정확히 입력하세요.
# B. 예약 취소하기
#     1. 문서ID는 "1lXs3JrOuvBSew2EJUZhEeaEQfGaSqIcuKcVicOkRxMQ" 시트의 이름은 "시트1"입니다.
#     2. get_sheet_data 툴을 활용하여 취소를 요청받은 이름을 탐색합니다.
#     3. 해당 이름이 있는 행의 정보를 지웁니다.
#     4. 하나의 행이 비게 되므로 그 아래 내용들을 위로 한 칸씩 당깁니다.
    
# </PROCESS>

# ----

# <OUTPUT_FORMAT>
# (concise answer to the question)

# **Source**(if applicable)
# - (source1: valid URL)
# - (source2: valid URL)
# - ...
# </OUTPUT_FORMAT>
# """

# class HospitalReservationAgent:
#     def __init__(self):
#         self.agent = None
#         self.session_id = None
#         self.config = None
#         self.client = None  # MCP 클라이언트를 인스턴스 변수로 관리
        
#     async def initialize(self, mcp_config=None):
#         """에이전트 초기화"""
#         try:
#             print("🔄 에이전트 초기화 시작...")
            
#             # config.json 파일 로드
#             if mcp_config is None:
#                 with open("config.json", 'r', encoding='utf-8') as f:
#                     mcp_config = json.load(f)
#                     print(f"✅ config.json 로드 완료: {mcp_config}")
            
#             # 세션 ID 생성 (대화 상태 유지를 위해)
#             self.session_id = str(uuid.uuid4())
#             print(f"🆔 세션 ID 생성: {self.session_id}")
            
#             # MCP 클라이언트 초기화
#             self.client = MultiServerMCPClient(mcp_config)
#             await self.client.__aenter__()
#             tools = self.client.get_tools()
#             print(f"🔧 도구 로드 완료: {len(tools)}개")
            
#             # 도구 목록 출력
#             print("📋 사용 가능한 도구들:")
#             for tool in tools:
#                 print(f"  - {tool.name}: {tool.description}")
            
#             # Google Sheets 관련 도구 확인
#             sheets_tools = [tool for tool in tools if 'sheet' in tool.name.lower()]
#             if sheets_tools:
#                 print(f"📊 Google Sheets 관련 도구: {len(sheets_tools)}개")
#                 for tool in sheets_tools:
#                     print(f"  - {tool.name}")
#             else:
#                 print("⚠️ Google Sheets 관련 도구를 찾을 수 없습니다!")
            
#             # OpenAI 모델 사용
#             model = ChatOpenAI(
#                 model='gpt-4o-mini',
#                 temperature=0.1,
#                 max_tokens=16000,
#             )
#             print("🤖 OpenAI 모델 초기화 완료")
            
#             # 에이전트 생성 (메모리 저장소 포함)
#             self.agent = create_react_agent(
#                 model,
#                 tools,
#                 checkpointer=MemorySaver(),
#                 prompt=SYSTEM_PROMPT,
#             )
#             print("🎯 에이전트 생성 완료")
            
#             # 설정 객체 생성
#             self.config = RunnableConfig(
#                 configurable={"thread_id": self.session_id}
#             )
            
#             print("🏥 병원 예약 에이전트가 초기화되었습니다!")
#             print("📋 예약하기, 예약 취소하기, 예약 조회하기 등의 서비스를 제공합니다.")
#             print("💬 대화를 시작하세요! ('quit' 또는 'exit'로 종료)")
#             print("-" * 50)
            
#         except Exception as e:
#             print(f"❌ 에이전트 초기화 실패: {str(e)}")
#             import traceback
#             traceback.print_exc()
#             raise
    
#     async def cleanup(self):
#         """리소스 정리"""
#         if self.client:
#             try:
#                 await self.client.__aexit__(None, None, None)
#                 print("🧹 MCP 클라이언트 정리 완료")
#             except Exception as e:
#                 print(f"⚠️ 클라이언트 정리 중 오류: {e}")
    
#     async def chat(self, message):
#         """사용자와 대화하기 - 수정된 버전"""
#         if not self.agent:
#             raise Exception("에이전트가 초기화되지 않았습니다. initialize()를 먼저 호출하세요.")
        
#         try:
#             # 사용자 메시지 생성
#             human_message = HumanMessage(content=message)
            
#             # 에이전트에게 메시지 전송 및 응답 받기
#             response = await self.agent.ainvoke(
#                 {"messages": [human_message]}, 
#                 config=self.config
#             )
            
#             # 응답에서 AI 메시지만 추출
#             if response and "messages" in response:
#                 # AI 메시지만 필터링 (도구 메시지 제외)
#                 ai_messages = [
#                     msg for msg in response["messages"] 
#                     if hasattr(msg, 'type') and msg.type == 'ai'
#                 ]
                
#                 if ai_messages:
#                     return ai_messages[-1].content
#                 else:
#                     # AI 메시지가 없는 경우 마지막 메시지 반환
#                     last_message = response["messages"][-1]
#                     if hasattr(last_message, 'content'):
#                         return last_message.content
#                     else:
#                         return "응답을 처리하는 중입니다."
#             else:
#                 return "죄송합니다. 응답을 생성할 수 없습니다."
                
#         except Exception as e:
#             error_msg = f"대화 중 오류가 발생했습니다: {str(e)}"
#             print(f"❌ {error_msg}")
#             import traceback
#             traceback.print_exc()
#             return error_msg
    
#     async def stream_chat(self, message):
#         """스트리밍 방식으로 대화하기 - 수정된 버전"""
#         if not self.agent:
#             raise Exception("에이전트가 초기화되지 않았습니다. initialize()를 먼저 호출하세요.")
        
#         try:
#             human_message = HumanMessage(content=message)
            
#             # 전체 응답을 위한 변수
#             full_response = ""
#             final_ai_content = ""
            
#             # # 일반 출력
#             # async for chunk in self.agent.astream(
#             #     {"messages": [human_message]}, 
#             #     config=self.config
#             # ):
#             #     # 에이전트의 최종 메시지 처리
#             #     if 'agent' in chunk:
#             #         messages = chunk['agent'].get('messages', [])
#             #         for msg in messages:
#             #             # AI 메시지만 처리
#             #             if hasattr(msg, 'type') and msg.type == 'ai':
#             #                 if hasattr(msg, 'content') and msg.content:
#             #                     final_ai_content = msg.content
#             #                     # print(msg.content)
                
#             #     # 도구 실행 결과 처리
#             #     elif 'tools' in chunk:
#             #         # 도구 실행 중임을 표시
#             #         print("🔧 도구 실행 중...", end="", flush=True)

#             # 토큰 출력
#             async for chunk in self.agent.astream(
#                 {"messages": [human_message]}, 
#                 stream_mode="messages",
#                 config=self.config
#             ):

#                 if isinstance(chunk[0], ToolMessage):
#                     continue

#                 # 에이전트 메시지 처리
#                 if 'tool_calls' in chunk[0].additional_kwargs:
#                     # pass
#                     print("🔧 도구 실행 중...", end="", flush=True)
#                 else:
#                     final_ai_content = chunk[0].content

#                 # 최종 응답 반환
#                 if type(final_ai_content) == str:
#                     yield final_ai_content
#                 else:
#                     yield "작업이 완료되었습니다."
            
#         except Exception as e:
#             error_msg = f"스트리밍 중 오류가 발생했습니다: {str(e)}"
#             print(f"❌ {error_msg}")
#             import traceback
#             traceback.print_exc()
#             yield error_msg

# async def test_sheet_access():
#     """Google Sheets 접근 테스트"""
#     try:
#         print("🔍 Google Sheets 접근 테스트 시작...")
        
#         # config.json 로드
#         with open("config.json", 'r', encoding='utf-8') as f:
#             mcp_config = json.load(f)
        
#         # MCP 클라이언트를 통한 접근
#         async with MultiServerMCPClient(mcp_config) as client:
#             tools = client.get_tools()
#             print(f"✅ 도구 로드 성공: {len(tools)}개")
            
#             # 시트 읽기 도구 찾기
#             sheet_tools = [tool for tool in tools if 'sheet' in tool.name.lower() or 'get' in tool.name.lower()]
            
#             if sheet_tools:
#                 print(f"📊 시트 관련 도구 발견: {len(sheet_tools)}개")
#                 for tool in sheet_tools:
#                     print(f"  - {tool.name}: {tool.description}")
                
#                 # 첫 번째 시트 도구로 테스트
#                 test_tool = sheet_tools[0]
#                 print(f"🧪 테스트 도구: {test_tool.name}")
                
#                 # 도구 스키마 상세 확인
#                 if hasattr(test_tool, 'inputSchema'):
#                     schema = test_tool.inputSchema
#                     print(f"📋 도구 스키마:")
#                     print(json.dumps(schema, indent=2, ensure_ascii=False))
                    
#                     # 필수 매개변수 확인
#                     if 'properties' in schema:
#                         required_params = schema.get('required', [])
#                         print(f"🔧 필수 매개변수: {required_params}")
#                         print("📝 매개변수 상세:")
#                         for prop_name, prop_info in schema['properties'].items():
#                             is_required = prop_name in required_params
#                             prop_type = prop_info.get('type', 'unknown')
#                             prop_desc = prop_info.get('description', '설명 없음')
#                             print(f"  - {prop_name} ({'필수' if is_required else '선택'}): {prop_type} - {prop_desc}")
                
#                 # 스프레드시트 ID와 시트명
#                 SPREADSHEET_ID = "1lXs3JrOuvBSew2EJUZhEeaEQfGaSqIcuKcVicOkRxMQ"
#                 SHEET_NAME = "시트1"
                
#                 # 다양한 매개변수 조합 시도
#                 param_combinations = [
#                     {"spreadsheet_id": SPREADSHEET_ID, "sheet": SHEET_NAME, "range": "A1:Z100"},
#                     {"spreadsheet_id": SPREADSHEET_ID, "sheet": SHEET_NAME},
#                     {"spreadsheet_id": SPREADSHEET_ID, "range": f"{SHEET_NAME}!A1:Z100"},
#                     {"spreadsheet_id": SPREADSHEET_ID, "sheet_name": SHEET_NAME, "range": "A1:Z100"},
#                     {"spreadsheet_id": SPREADSHEET_ID, "range": "A1:Z100"},
#                 ]
                
#                 for i, params in enumerate(param_combinations):
#                     try:
#                         print(f"🔄 시도 {i+1}: {params}")
                        
#                         # ainvoke 시도
#                         if hasattr(test_tool, 'ainvoke'):
#                             result = await test_tool.ainvoke(params)
#                             print(f"✅ 시트 읽기 성공!")
                            
#                             # 결과 처리
#                             if hasattr(result, 'content'):
#                                 content = result.content
#                                 if isinstance(content, list) and content:
#                                     for item in content:
#                                         if hasattr(item, 'text'):
#                                             print(f"📊 데이터 샘플:\n{item.text[:500]}...")
#                                             return True
#                                         else:
#                                             print(f"📊 데이터 샘플:\n{str(item)[:500]}...")
#                                             return True
#                                 elif isinstance(content, str):
#                                     print(f"📊 데이터 샘플:\n{content[:500]}...")
#                                     return True
#                             elif isinstance(result, str):
#                                 print(f"📊 데이터 샘플:\n{result[:500]}...")
#                                 return True
                            
#                             break
                            
#                     except Exception as e:
#                         print(f"❌ 시도 {i+1} 실패: {str(e)}")
#                         continue
                
#                 print("⚠️ 모든 시도 실패")
#                 return False
#             else:
#                 print("❌ 시트 관련 도구를 찾을 수 없습니다!")
#                 return False
                
#     except Exception as e:
#         print(f"❌ 시트 접근 테스트 실패: {str(e)}")
#         import traceback
#         traceback.print_exc()
#         return False

# async def run_interactive_chat():
#     """대화형 인터페이스 실행"""
#     agent = HospitalReservationAgent()
    
#     try:
#         # 에이전트 초기화
#         await agent.initialize()
        
#         while True:
#             try:
#                 # 사용자 입력 받기
#                 user_input = input("\n🔹 사용자: ").strip()
                
#                 # 종료 명령 확인
#                 if user_input.lower() in ['quit', 'exit', '종료', '나가기']:
#                     print("👋 감사합니다. 좋은 하루 되세요!")
#                     break
                
#                 # 빈 입력 무시
#                 if not user_input:
#                     continue
                
#                 # 에이전트 응답 출력 - 일반 모드로 변경
#                 # print("🤖 에이전트: ", end="")
#                 # response = await agent.stream_chat(user_input)
#                 # print(response)

#                 #토큰 출력
#                 print("🤖 에이전트: ", end="")
#                 async for token in agent.stream_chat(user_input):
#                     print(token, end="", flush=True)
                
#             except KeyboardInterrupt:
#                 print("\n\n👋 대화가 중단되었습니다. 좋은 하루 되세요!")
#                 break
#             except Exception as e:
#                 print(f"\n⚠️ 대화 중 오류가 발생했습니다: {str(e)}")
#                 continue
                
#     except Exception as e:
#         print(f"❌ 시스템 오류: {str(e)}")
#         import traceback
#         traceback.print_exc()
#     finally:
#         # 리소스 정리
#         await agent.cleanup()

# async def main():
#     """메인 함수"""
#     print("🏥 병원 예약 에이전트 시스템 (수정된 버전)")
#     print("=" * 50)
    
#     # 운영 체제별 이벤트 루프 정책 설정
#     if platform.system() == 'Windows':
#         asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
#     # nest_asyncio 적용 (필요한 경우)
#     try:
#         nest_asyncio.apply()
#         print("🔄 nest_asyncio 적용 완료")
#     except:
#         print("ℹ️ nest_asyncio 적용 건너뜀")
    
#     # 대화형 모드 실행
#     await run_interactive_chat()

# if __name__ == "__main__":
#     asyncio.run(main())