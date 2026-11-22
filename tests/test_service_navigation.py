from tests.test_assistant_web import client, login, start_chat
import pytest
from app.assistant.navigation import requested_service
@pytest.mark.parametrize('question',['Открой отметку на паре','Открой, пожалуйста, почту','Как открыть почту?','Не открывай почту','Открой https://example.com'])
def test_navigation_allowlist(question):
    result=requested_service(question,'student')
    if question=='Открой отметку на паре':assert result['href']=='/attendance'
    elif question=='Открой, пожалуйста, почту':assert result['href']=='/services#service-3'
    else:assert result is None
async def test_navigation_without_llm(client):
    csrf=await login(client);token=await start_chat(client,csrf)
    r=await client.post('/s/assistant/ask',json={'question':'Открой отметку на паре','conversation_token':token},headers={'X-CSRF-Token':csrf})
    assert r.status_code==200 and r.json()['navigation']['href']=='/attendance'
