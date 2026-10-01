"""Typed MCP tool boundary. Approval/execution is deliberately not a model tool."""
import json
import re
from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field
from ..domain.engine import calculate, require

class StrictInput(BaseModel):
    model_config=ConfigDict(extra='forbid')

class PortfolioInput(StrictInput):
    customer_id: Literal['cus_demo_7']='cus_demo_7'

class GapInput(StrictInput):
    goal_id: Literal['house','retirement']
    scenario_id: Literal['flat_return_fixture','stress_fixture']='flat_return_fixture'

class ProposalInput(StrictInput):
    goal_version: int
    mandate_version: int
    contribution_minor: int=Field(ge=0,le=10000000)

class PreviewInput(StrictInput):
    proposal_id: str

SCHEMAS={'get_goal_portfolio':PortfolioInput,'calculate_goal_gap':GapInput,'propose_rebalance':ProposalInput,
         'preview_action_basket':PreviewInput,'reconcile_portfolio':PreviewInput}
DESCRIPTIONS={
    'get_goal_portfolio':'Read the synthetic customer goals, restrictions and timestamped valuations.',
    'calculate_goal_gap':'Calculate a reproducible funding gap using the named assumption set.',
    'propose_rebalance':'Prepare a contribution-first draft. Does not grant approval or execute.',
    'preview_action_basket':'Read exact action amounts, assumptions, account constraints and approval hash.',
    'reconcile_portfolio':'Read verified reconciliation evidence; never claims an unverified completion.'}

class ToolGateway:
    def __init__(self,service):
        self.service=service

    def definitions(self):
        return [{'name':name,'description':DESCRIPTIONS[name],'inputSchema':schema.model_json_schema(),
                 'annotations':{'readOnlyHint':name!='propose_rebalance','destructiveHint':False}} for name,schema in SCHEMAS.items()]

    def call(self,name,arguments):
        require(name in SCHEMAS,'Unknown tool.',404)
        args=SCHEMAS[name].model_validate(arguments).model_dump()
        s=self.service.state()
        if name=='get_goal_portfolio':
            result={key:s[key] for key in ('goals','accounts','mandate','clock','holdings_version')}
        elif name=='calculate_goal_gap':
            result=calculate(s,**args)
        elif name=='propose_rebalance':
            require(args['goal_version']==s['goals'][0]['version'] and args['mandate_version']==s['mandate']['version'],'Tool input versions are stale.',409)
            result=self.service.propose(s['version'],args['contribution_minor'])
        else:
            p=self.service.find_proposal(s,args['proposal_id'])
            result=p if name=='preview_action_basket' else {'status':p['status'],'reconciliation':p['reconciliation']}
        with self.service.store.transaction() as db:
            db.execute('INSERT INTO tool_runs(customer_id,name,at,outcome) VALUES(?,?,?,?)',(s['customer_id'],name,s['clock'],'success'))
        return {'source':'deterministic-wealth-engine','retrieved_at':s['clock'],'authority':'simulated','environment':'mock','data':result}

class PlannerProvider(Protocol):
    def respond(self,message: str,gateway: ToolGateway) -> dict: ...

class RulesPlanner:
    """Credential-free reference planner, maximum five typed calls per turn."""
    max_tool_calls=5
    def respond(self,message,gateway):
        text=message.lower()
        calls=[]
        def invoke(name,args):
            require(len(calls)<self.max_tool_calls,'Tool budget exhausted; review the case manually.')
            result=gateway.call(name,args)
            calls.append(name)
            return result['data']
        if any(w in text for w in ('ignore instruction','ignore all','system prompt','secret','other customer','another customer','cus_other')):
            return {'reply':'I can only use this synthetic customer’s scoped planning tools. Documents and messages cannot change permissions.','outcome':'refusal','tool_calls':calls}
        if any(w in text for w in ('withdraw','sell','execute','buy stock','guarantee','approve for me','trade for me','tax loss')):
            return {'reply':'I can prepare a plan for review. Sales, retirement withdrawals and guaranteed returns are outside this prototype. Every supported basket needs your separate approval.','outcome':'refusal','tool_calls':calls}
        if any(w in text for w in ('conflict','statement says','different balance')):
            return {'reply':'The figures conflict with the current verified fixture. Refresh holdings and review the source evidence before preparing a basket. I will not treat an unverified statement as authoritative.','outcome':'conflicting_evidence','tool_calls':calls}
        portfolio=invoke('get_goal_portfolio',{})
        goal=portfolio['goals'][0]
        scenario='stress_fixture' if 'stress' in text else 'flat_return_fixture'
        gap=invoke('calculate_goal_gap',{'goal_id':'house','scenario_id':scenario})
        if any(w in text for w in ('earlier','one year','12 months','next year')):
            year=int(goal['target_date'][:4])-1
            target=str(year)+goal['target_date'][4:]
            try:
                preview=gateway.service.preview(target,goal['monthly_minor'],scenario)
                amount=preview['after']['required_monthly_minor']/100
                return {'reply':f'Moving your house goal to {target} requires ${amount:,.0f} per month, up from ${gap["required_monthly_minor"]/100:,.0f}, under the {gap["assumptions"]["name"].lower()} scenario. Your allocation mandate stays the same. Review the date and monthly budget before saving.',
                        'outcome':'revision_suggested','suggested_date':target,'suggested_monthly_minor':preview['after']['required_monthly_minor'],'tool_calls':calls}
            except Exception:
                return {'reply':'That date leaves less than one contribution month. Choose a later target date or revise the amount.','outcome':'missing_information','tool_calls':calls}
        if any(w in text for w in ('only','afford','budget','insufficient','shortfall')):
            number=re.search(r'\$([\d,]+)',text)
            budget=int(number.group(1).replace(',',''))*100 if number else goal['monthly_minor']
            result=gateway.service.preview(goal['target_date'],budget,scenario)['after']
            return {'reply':f'At ${budget/100:,.0f} a month, the remaining shortfall is ${result["shortfall_minor"]/100:,.0f}. You can change your monthly budget, target amount or target date. I won’t assume a higher return to close the gap.',
                    'outcome':'funding_gap','tool_calls':calls}
        if any(w in text for w in ('partial','failed','pending','timeout','status','reconcile')):
            state=gateway.service.state()
            if state['proposals']:
                result=invoke('reconcile_portfolio',{'proposal_id':state['proposals'][-1]['id']})
                return {'reply':f'The latest basket is {result["status"].replace("_"," ")}. Completed provider actions remain in the portfolio. A failed remainder needs a fresh proposal from verified holdings.',
                        'outcome':'status','tool_calls':calls}
        if any(w in text for w in ('plan','goal','house','stress','contribution','allocation','hello','hi')):
            return {'reply':f'Your house goal has ${gap["current_minor"]/100:,.0f} earmarked toward ${gap["target_minor"]/100:,.0f}. Over {gap["months"]} months, you need ${gap["required_monthly_minor"]/100:,.0f} a month under {gap["assumptions"]["name"].lower()} assumptions. {gap["assumptions"]["description"]}',
                    'outcome':'analysis','tool_calls':calls}
        return {'reply':'Would you like to change the house date, check an affordable monthly contribution, or inspect the latest basket?','outcome':'missing_information','tool_calls':calls}
