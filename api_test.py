import asyncio
import aiohttp
import json
import sys

class HospitalAgentClient:
    def __init__(self, base_url="http://localhost:8090"):
        self.base_url = base_url
    
    async def test_health(self):
        """헬스 체크 테스트"""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{self.base_url}/health") as response:
                    if response.status == 200:
                        data = await response.json()
                        print(f"✅ 헬스 체크 성공: {data}")
                        return True
                    else:
                        print(f"❌ 헬스 체크 실패: {response.status}")
                        return False
            except Exception as e:
                print(f"❌ 헬스 체크 오류: {e}")
                return False
    
    async def simple_chat(self, message):
        """간단한 채팅 테스트"""
        async with aiohttp.ClientSession() as session:
            try:
                payload = {"message": message}
                async with session.post(
                    f"{self.base_url}/chat",
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        print(f"🤖 응답: {data['response']}")
                        return data['response']
                    else:
                        error_text = await response.text()
                        print(f"❌ 채팅 실패: {response.status} - {error_text}")
                        return None
            except Exception as e:
                print(f"❌ 채팅 오류: {e}")
                return None
    
    async def openai_chat(self, messages, stream=False):
        """OpenAI 호환 API 테스트"""
        async with aiohttp.ClientSession() as session:
            try:
                payload = {
                    "model": "gpt-4o-mini",
                    "messages": messages,
                    "stream": stream,
                    "temperature": 0.1
                }
                
                async with session.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    
                    if response.status == 200:
                        if stream:
                            print("🔄 스트리밍 응답:")
                            async for line in response.content:
                                line = line.decode('utf-8').strip()
                                if line.startswith('data: '):
                                    data_part = line[6:]  # 'data: ' 제거
                                    if data_part == '[DONE]':
                                        print("\n✅ 스트리밍 완료")
                                        break
                                    try:
                                        chunk = json.loads(data_part)
                                        if 'choices' in chunk and chunk['choices']:
                                            delta = chunk['choices'][0].get('delta', {})
                                            content = delta.get('content', '')
                                            if content:
                                                print(content, end='', flush=True)
                                    except json.JSONDecodeError:
                                        continue
                        else:
                            data = await response.json()
                            if 'choices' in data and data['choices']:
                                message = data['choices'][0]['message']['content']
                                print(f"🤖 응답: {message}")
                                return message
                    else:
                        error_text = await response.text()
                        print(f"❌ OpenAI API 실패: {response.status} - {error_text}")
                        return None
                        
            except Exception as e:
                print(f"❌ OpenAI API 오류: {e}")
                return None
    
    async def interactive_chat(self):
        """대화형 채팅"""
        print("🏥 병원 예약 에이전트와 대화를 시작합니다!")
        print("💬 메시지를 입력하세요 ('quit' 또는 'exit'로 종료)")
        print("-" * 50)
        
        while True:
            try:
                user_input = input("\n🔹 사용자: ").strip()
                
                if user_input.lower() in ['quit', 'exit', '종료', '나가기']:
                    print("👋 감사합니다. 좋은 하루 되세요!")
                    break
                
                if not user_input:
                    continue
                
                # OpenAI API 스타일로 스트리밍 테스트
                messages = [{"role": "user", "content": user_input}]
                
                print("🤖 에이전트: ", end="")
                await self.openai_chat(messages, stream=True)
                
            except KeyboardInterrupt:
                print("\n👋 대화가 중단되었습니다.")
                break
            except Exception as e:
                print(f"⚠️ 오류: {e}")

async def main():
    """메인 테스트 함수"""
    client = HospitalAgentClient()
    
    print("🧪 API 테스트 시작")
    print("=" * 50)
    
    # 1. 헬스 체크
    print("1️⃣ 헬스 체크 테스트")
    health_ok = await client.test_health()
    
    if not health_ok:
        print("❌ 서버가 실행되지 않았거나 문제가 있습니다.")
        return
    
    # 2. 간단한 채팅 테스트
    print("\n2️⃣ 간단한 채팅 테스트")
    await client.simple_chat("안녕하세요!")
    
    # 3. OpenAI API 테스트 (일반 응답)
    print("\n3️⃣ OpenAI API 테스트 (일반 응답)")
    messages = [{"role": "user", "content": "병원 예약 시스템에 대해 설명해주세요."}]
    await client.openai_chat(messages, stream=False)
    
    # 4. OpenAI API 테스트 (스트리밍)
    print("\n4️⃣ OpenAI API 테스트 (스트리밍)")
    messages = [{"role": "user", "content": "예약 가능한 시간을 알려주세요."}]
    await client.openai_chat(messages, stream=True)
    
    # 5. 대화형 모드
    print("\n5️⃣ 대화형 모드로 전환할까요? (y/n)")
    response = input().strip().lower()
    if response == 'y':
        await client.interactive_chat()

if __name__ == "__main__":
    print("🏥 병원 예약 에이전트 API 클라이언트")
    print("=" * 50)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 테스트가 중단되었습니다.")
    except Exception as e:
        print(f"❌ 테스트 실행 중 오류: {e}")