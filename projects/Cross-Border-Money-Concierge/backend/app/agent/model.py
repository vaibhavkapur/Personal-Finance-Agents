"""Optional model seam. Models propose typed calls; only ordinary code dispatches them."""
from typing import Protocol
from pydantic import BaseModel, ConfigDict, Field
from ..domain.lifecycle import require

class ToolProposal(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str
    arguments: dict=Field(default_factory=dict)

class StructuredModel(Protocol):
    def next_tool(self, messages: list, tools: list) -> ToolProposal: ...

class BoundedOrchestrator:
    def __init__(self, model: StructuredModel, tools, budget=5):
        require(0 < budget <= 5, 'Tool budget exceeds the allowed maximum', 422)
        self.model,self.tools,self.budget=model,tools,budget

    def run(self, user, messages):
        evidence=[]
        for _ in range(self.budget):
            proposal=self.model.next_tool(messages+evidence,self.tools.catalog())
            if proposal.name=='finish':
                return {'evidence':evidence,'needs_review':False}
            require(proposal.name in {t['name'] for t in self.tools.catalog()}, 'Tool is not permitted', 403)
            result=self.tools.call(user,proposal.name,proposal.arguments)
            evidence.append({'role':'tool','name':proposal.name,'content':result})
        return {'evidence':evidence,'needs_review':True,'reason':'Tool budget exhausted; unresolved work requires review.'}
