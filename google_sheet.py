import asyncio
import nest_asyncio
import json
import os
import platform
import uuid

from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages.ai import AIMessageChunk
from langchain_core.messages.tool import ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.runnables import RunnableConfig

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = "/home/ubuntu/Desktop/mcp_sheets.json"

# 환경 변수 로드 (.env 파일에서 API 키 등의 설정을 가져옴)
load_dotenv(override=True)

# config.json 파일 경로 설정
CONFIG_FILE_PATH = "config.json"

# JSON 설정 파일 로드 함수
def load_config_from_json():
    """
    config.json 파일에서 설정을 로드합니다.
    파일이 없는 경우 기본 설정으로 파일을 생성합니다.

    반환값:
        dict: 로드된 설정
    """
    default_config = {
        "get_current_time": {
            "command": "python",
            "args": ["./mcp_server_time.py"],
            "transport": "stdio"
        }
    }
    
    try:
        if os.path.exists(CONFIG_FILE_PATH):
            with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        else:
            # 파일이 없는 경우 기본 설정으로 파일 생성
            save_config_to_json(default_config)
            return default_config
    except Exception as e:
        print(f"설정 파일 로드 중 오류 발생: {str(e)}")
        return default_config

# JSON 설정 파일 저장 함수
def save_config_to_json(config):
    """
    설정을 config.json 파일에 저장합니다.

    매개변수:
        config (dict): 저장할 설정
    
    반환값:
        bool: 저장 성공 여부
    """
    try:
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"설정 파일 저장 중 오류 발생: {str(e)}")
        return False


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
        self.session_id = None
        self.config = None
        self.client = None  # MCP 클라이언트를 인스턴스 변수로 관리
        
    async def initialize(self, mcp_config=None):
        """에이전트 초기화"""
        try:
            print("🔄 에이전트 초기화 시작...")
            
            # config.json 파일 로드
            if mcp_config is None:
                with open("config.json", 'r', encoding='utf-8') as f:
                    mcp_config = json.load(f)
                    print(f"✅ config.json 로드 완료: {mcp_config}")
            
            # 세션 ID 생성 (대화 상태 유지를 위해)
            self.session_id = str(uuid.uuid4())
            print(f"🆔 세션 ID 생성: {self.session_id}")
            
            # MCP 클라이언트 초기화 - 첫 번째 코드의 성공 패턴 사용
            self.client = MultiServerMCPClient(mcp_config)
            
            # 컨텍스트 매니저 없이 클라이언트 시작
            await self.client.__aenter__()
            tools = self.client.get_tools()
            print(f"🔧 도구 로드 완료: {len(tools)}개")
            
            # 도구 목록 출력
            print("📋 사용 가능한 도구들:")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")
            
            # Google Sheets 관련 도구 확인
            sheets_tools = [tool for tool in tools if 'sheet' in tool.name.lower()]
            if sheets_tools:
                print(f"📊 Google Sheets 관련 도구: {len(sheets_tools)}개")
                for tool in sheets_tools:
                    print(f"  - {tool.name}")
            else:
                print("⚠️ Google Sheets 관련 도구를 찾을 수 없습니다!")
            
            # OpenAI 모델 사용
            model = ChatOpenAI(
                model='gpt-4o-mini',
                temperature=0.1,
                max_tokens=16000,
            )
            print("🤖 OpenAI 모델 초기화 완료")
            
            # 에이전트 생성 (메모리 저장소 포함)
            self.agent = create_react_agent(
                model,
                tools,
                checkpointer=MemorySaver(),
                prompt=SYSTEM_PROMPT,
            )
            print("🎯 에이전트 생성 완료")
            
            # 설정 객체 생성
            self.config = RunnableConfig(
                configurable={"thread_id": self.session_id}
            )
            
            print("🏥 병원 예약 에이전트가 초기화되었습니다!")
            print("📋 예약하기, 예약 취소하기, 예약 조회하기 등의 서비스를 제공합니다.")
            print("💬 대화를 시작하세요! ('quit' 또는 'exit'로 종료)")
            print("-" * 50)
            
        except Exception as e:
            print(f"❌ 에이전트 초기화 실패: {str(e)}")
            import traceback
            traceback.print_exc()
            raise
    
    async def cleanup(self):
        """리소스 정리"""
        if self.client:
            try:
                await self.client.__aexit__(None, None, None)
                print("🧹 MCP 클라이언트 정리 완료")
            except Exception as e:
                print(f"⚠️ 클라이언트 정리 중 오류: {e}")
    
    async def chat(self, message):
        """사용자와 대화하기"""
        if not self.agent:
            raise Exception("에이전트가 초기화되지 않았습니다. initialize()를 먼저 호출하세요.")
        
        try:
            # 사용자 메시지 생성
            human_message = HumanMessage(content=message)
            
            # 에이전트에게 메시지 전송 및 응답 받기
            response = await self.agent.ainvoke(
                {"messages": [human_message]}, 
                config=self.config
            )
            
            # 에이전트의 마지막 메시지 반환
            if response and "messages" in response:
                last_message = response["messages"][-1]
                return last_message.content
            else:
                return "죄송합니다. 응답을 생성할 수 없습니다."
                
        except Exception as e:
            error_msg = f"대화 중 오류가 발생했습니다: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
            return error_msg
    
    async def stream_chat(self, message):
        """스트리밍 방식으로 대화하기"""
        if not self.agent:
            raise Exception("에이전트가 초기화되지 않았습니다. initialize()를 먼저 호출하세요.")
        
        try:
            human_message = HumanMessage(content=message)
            
            # # 토큰 출력
            # async for chunk in self.agent.astream(
            #     {"messages": [human_message]}, 
            #     stream_mode="messages",
            #     config=self.config
            # ):
            #     # 에이전트 메시지 처리
            #     if 'tool_calls' in chunk[0].additional_kwargs:
            #         print()
            #     else:
            #         token = chunk[0].content
            #         return token
            
            # 전체 출력
            async for chunk in self.agent.astream(
                {"messages": [human_message]}, 
                config=self.config
            ):
                # 에이전트 메시지 처리
                if 'agent' in chunk:
                    print(chunk['agent']['messages'][0].content)
                    return chunk['agent']['messages'][0].content
                else:
                    print()

            
        except Exception as e:
            error_msg = f"스트리밍 중 오류가 발생했습니다: {str(e)}"
            print(f"❌ {error_msg}")


# 첫 번째 코드의 성공적인 시트 읽기 테스트 함수 추가
async def test_sheet_access():
    """Google Sheets 접근 테스트"""
    try:
        print("🔍 Google Sheets 접근 테스트 시작...")
        
        # config.json 로드
        with open("config.json", 'r', encoding='utf-8') as f:
            mcp_config = json.load(f)
        
        # MCP 클라이언트를 통한 접근
        async with MultiServerMCPClient(mcp_config) as client:
            tools = client.get_tools()
            print(f"✅ 도구 로드 성공: {len(tools)}개")
            
            # 시트 읽기 도구 찾기
            sheet_tools = [tool for tool in tools if 'sheet' in tool.name.lower() or 'get' in tool.name.lower()]
            
            if sheet_tools:
                print(f"📊 시트 관련 도구 발견: {len(sheet_tools)}개")
                for tool in sheet_tools:
                    print(f"  - {tool.name}: {tool.description}")
                
                # 첫 번째 시트 도구로 테스트
                test_tool = sheet_tools[0]
                print(f"🧪 테스트 도구: {test_tool.name}")
                
                # 도구 스키마 상세 확인
                if hasattr(test_tool, 'inputSchema'):
                    schema = test_tool.inputSchema
                    print(f"📋 도구 스키마:")
                    print(json.dumps(schema, indent=2, ensure_ascii=False))
                    
                    # 필수 매개변수 확인
                    if 'properties' in schema:
                        required_params = schema.get('required', [])
                        print(f"🔧 필수 매개변수: {required_params}")
                        print("📝 매개변수 상세:")
                        for prop_name, prop_info in schema['properties'].items():
                            is_required = prop_name in required_params
                            prop_type = prop_info.get('type', 'unknown')
                            prop_desc = prop_info.get('description', '설명 없음')
                            print(f"  - {prop_name} ({'필수' if is_required else '선택'}): {prop_type} - {prop_desc}")
                
                # 스프레드시트 ID와 시트명
                SPREADSHEET_ID = "1lXs3JrOuvBSew2EJUZhEeaEQfGaSqIcuKcVicOkRxMQ"
                SHEET_NAME = "시트1"
                
                # 다양한 매개변수 조합 시도 (오류 메시지에서 'sheet' 필드가 필수라고 나옴)
                param_combinations = [
                    {"spreadsheet_id": SPREADSHEET_ID, "sheet": SHEET_NAME, "range": "A1:Z100"},
                    {"spreadsheet_id": SPREADSHEET_ID, "sheet": SHEET_NAME},
                    {"spreadsheet_id": SPREADSHEET_ID, "range": f"{SHEET_NAME}!A1:Z100"},
                    {"spreadsheet_id": SPREADSHEET_ID, "sheet_name": SHEET_NAME, "range": "A1:Z100"},
                    {"spreadsheet_id": SPREADSHEET_ID, "range": "A1:Z100"},
                ]
                
                for i, params in enumerate(param_combinations):
                    try:
                        print(f"🔄 시도 {i+1}: {params}")
                        
                        # ainvoke 시도
                        if hasattr(test_tool, 'ainvoke'):
                            result = await test_tool.ainvoke(params)
                            print(f"✅ 시트 읽기 성공!")
                            
                            # 결과 처리
                            if hasattr(result, 'content'):
                                content = result.content
                                if isinstance(content, list) and content:
                                    for item in content:
                                        if hasattr(item, 'text'):
                                            print(f"📊 데이터 샘플:\n{item.text[:500]}...")
                                            return True
                                        else:
                                            print(f"📊 데이터 샘플:\n{str(item)[:500]}...")
                                            return True
                                elif isinstance(content, str):
                                    print(f"📊 데이터 샘플:\n{content[:500]}...")
                                    return True
                            elif isinstance(result, str):
                                print(f"📊 데이터 샘플:\n{result[:500]}...")
                                return True
                            
                            break
                            
                    except Exception as e:
                        print(f"❌ 시도 {i+1} 실패: {str(e)}")
                        continue
                
                print("⚠️ 모든 시도 실패")
                return False
            else:
                print("❌ 시트 관련 도구를 찾을 수 없습니다!")
                return False
                
    except Exception as e:
        print(f"❌ 시트 접근 테스트 실패: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

async def run_interactive_chat():
    """대화형 인터페이스 실행"""
    agent = HospitalReservationAgent()
    
    try:
        # # 먼저 시트 접근 테스트
        # print("🔍 Google Sheets 접근 테스트 중...")
        # sheet_test_result = await test_sheet_access()
        
        # if not sheet_test_result:
        #     print("⚠️ Google Sheets 접근에 문제가 있을 수 있습니다.")
        #     response = input("계속 진행하시겠습니까? (y/n): ")
        #     if response.lower() != 'y':
        #         return
        
        # 에이전트 초기화
        await agent.initialize()
        
        while True:
            try:
                # 사용자 입력 받기
                user_input = input("\n🔹 사용자: ").strip()
                
                # 종료 명령 확인
                if user_input.lower() in ['quit', 'exit', '종료', '나가기']:
                    print("👋 감사합니다. 좋은 하루 되세요!")
                    break
                
                # 빈 입력 무시
                if not user_input:
                    continue
                
                # 에이전트 응답 출력
                print("🤖 에이전트: ", end="")
                # 전체 출력
                await agent.stream_chat(user_input)
                
                #토큰 출력
                # async for token in agent.stream_chat(user_input):
                #     print(token, end="", flush=True)
                
            except KeyboardInterrupt:
                print("\n\n👋 대화가 중단되었습니다. 좋은 하루 되세요!")
                break
            except Exception as e:
                print(f"\n⚠️ 대화 중 오류가 발생했습니다: {str(e)}")
                continue
                
    except Exception as e:
        print(f"❌ 시스템 오류: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        # 리소스 정리
        await agent.cleanup()

async def main():
    """메인 함수"""
    print("🏥 병원 예약 에이전트 시스템 (수정된 버전)")
    print("=" * 50)
    
    # 운영 체제별 이벤트 루프 정책 설정
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    # nest_asyncio 적용 (필요한 경우)
    try:
        nest_asyncio.apply()
        print("🔄 nest_asyncio 적용 완료")
    except:
        print("ℹ️ nest_asyncio 적용 건너뜀")
    
    # 대화형 모드 실행
    await run_interactive_chat()

if __name__ == "__main__":
    asyncio.run(main())