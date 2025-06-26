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

# 환경 변수 설정
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = "/home/ubuntu/Desktop/mcp_sheets.json"
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

# 시스템 프롬프트
SYSTEM_PROMPT = """<ROLE>
You are a hospital reservation agent with an ability to use tools. 
You will be given a question and you will use the tools to answer the question.
Pick the most relevant tool to answer the question. 
If you are failed to answer the question, try different tools to get context.
Your answer should be very polite and professional.
</ROLE>

----

<INSTRUCTIONS>
Step 1: Analyze the question
- Analyze user's question and final goal.
- If the user's question is consist of multiple sub-questions, split them into smaller sub-questions.

Step 2: Pick the most relevant tool
- Pick the most relevant tool to answer the question.
- If you are failed to answer the question, try different tools to get context.

Step 3: Answer the question
- Answer the question in the same language as the question.
- Your answer should be very polite and professional.

Step 4: Provide the source of the answer(if applicable)
- If you've used the tool, provide the source of the answer.
- Valid sources are either a website(URL) or a document(PDF, etc).

Guidelines:
- If you've used the tool, your answer should be based on the tool's output(tool's output is more important than your own knowledge).
- If you've used the tool, and the source is valid URL, provide the source(URL) of the answer.
- Skip providing the source if the source is not URL.
- Answer in the same language as the question.
- Answer should be concise and to the point.
- Avoid response your output with any other information than the answer and the source.  
</INSTRUCTIONS>

<PROCESS>
A. 예약하기
    1. 문서ID는 "1lXs3JrOuvBSew2EJUZhEeaEQfGaSqIcuKcVicOkRxMQ" 시트의 이름은 "시트1"입니다.
    2. 성명, 생년월일, 예약일, 예약시간은 필수요소입니다. 정보가 부족하다면 정중하게 요청하세요.
    3. 필요한 정보가 다 수집되었다면 get_sheet_data 툴을 활용하여 기존 예약 목록을 확인하세요.
    4. 동일한 예약일과 예약시간에 이미 예약된 정보가 존재한다면, 예약을 절대 진행하지 마세요.
       - 중복 예약이 감지되었을 경우 반드시 다음과 같이 응답하세요:
         - "해당 시간에는 이미 예약이 있습니다. 다른 시간대를 선택해주세요."
       - 가능한 다른 시간대를 2~3개 추천하세요.
    5. 중복 예약이 없음을 확인한 후, 빈 행을 탐색하여 해당 위치에 정보를 기입하세요.
    6. update_cells 툴을 활용하여 새로운 예약정보를 정확히 입력하세요.
B. 예약 취소하기
    1. 문서ID는 "1lXs3JrOuvBSew2EJUZhEeaEQfGaSqIcuKcVicOkRxMQ" 시트의 이름은 "시트1"입니다.
    2. get_sheet_data 툴을 활용하여 취소를 요청받은 이름을 탐색합니다.
    3. 해당 이름이 있는 행의 정보를 지웁니다.
    4. 하나의 행이 비게 되므로 그 아래 내용들을 위로 한 칸씩 당깁니다.
    
</PROCESS>

----

<OUTPUT_FORMAT>
(concise answer to the question)

**Source**(if applicable)
- (source1: valid URL)
- (source2: valid URL)
- ...
</OUTPUT_FORMAT>
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
            # 마지막 사용자 메시지 추출
            user_message = None
            for msg in reversed(messages):
                if msg.role == "user":
                    user_message = msg.content
                    break
            
            if not user_message:
                yield "사용자 메시지를 찾을 수 없습니다."
                return
            
            human_message = HumanMessage(content=user_message)
            config = self.get_session_config(session_id)
            
            # 스트리밍 응답
            async for chunk in self.agent.astream(
                {"messages": [human_message]}, 
                stream_mode="messages",
                config=config
            ):
                # 에이전트 메시지 처리
                if 'tool_calls' in chunk[0].additional_kwargs:
                    print()
                else:
                    token = chunk[0].content
                    yield token
                
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
async def chat_completions(request: ChatCompletionRequest):
    """OpenAI 호환 채팅 완료 엔드포인트"""
    try:
        agent = await get_agent()
        
        # 세션 ID 생성 (실제 구현에서는 요청에서 추출하거나 사용자 인증을 통해 설정)
        session_id = str(uuid.uuid4())
        
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
        port=8090,
        reload=True,
        log_level="info"
    )

