export type Check = { code: string; passed: boolean; detail: string };
export type Risk = { allowed: boolean; checks: Check[]; reasons: Check[]; quantity: number; limit_price_minor: number; cash_reservation_minor: number; notional_minor: number; fee_buffer_minor: number };
export type Quote = { id: string; symbol: string; name: string; ask_minor: number; bid_minor: number; previous_close_minor: number; observed_at: string; information_cutoff: string; eligible_execution_at: string; bars: { at: string; close_minor: number }[] };
export type Signal = { id: string; symbol: string; direction: string; rationale: string; created_at: string; information_cutoff: string; eligible_execution_at: string; risk: Risk; inputs: { close_minor: number; sma_minor: number } };
export type Action = { id: string; type: string; status: string; payload_hash: string; challenge_id: string; expires_at: string; wall_expires_at: string; provider_reference?: string; error?: string; payload: { remaining_quantity_at_review?: number; filled_quantity_at_review?: number; effect?: string } };
export type Fill = { id: string; broker_execution_id: string; quantity: number; price_minor: number; fee_minor: number; executed_at: string };
export type Order = { id: string; symbol: string; status: string; quantity: number; filled_quantity: number; limit_price_minor: number; fees_minor: number; version: number; created_at: string; client_order_id: string; broker_order_id?: string; scenario: string; action: Action; cancel_action?: Action; fills: Fill[]; risk_at_review: Risk; reservation: { cash_minor: number; status: string } };
export type Mandate = { id: string; name: string; version: number; symbols: string[]; expires_at: string; revoked_at?: string; parameters: { lookback: 5; target_order_minor: number }; limits: { per_order_minor: number; per_symbol_minor: number; aggregate_minor: number; daily_turnover_minor: number; max_quote_age_seconds: number; limit_tolerance_bps: number } };
export type DeskEvent = { id: string; seq: number; event_type: string; actor: string; occurred_at: string; case_id?: string; data: { message?: string; symbol?: string; order_id?: string; previous_state?: string; next_state?: string; evidence_reference?: string } };
export type Desk = {
  runtime: { now: string; kill_switch: boolean; fixture_version: string };
  account: { cash_minor: number; reconciliation_status: string };
  mandate: Mandate; market: Quote[]; signals: Signal[]; orders: Order[]; events: DeskEvent[];
  positions: { symbol: string; quantity: number; cost_minor: number; market_value_minor: number; mark_minor: number; unrealized_pnl_minor: number }[];
  metrics: { equity_minor: number; exposure_minor: number; reserved_minor: number; buying_power_minor: number; unrealized_pnl_minor: number; pending_approvals: number; fees_minor: number };
  jobs: { id: string; action_id: string; state: string; attempts: number; error?: string }[];
  tool_runs: { id: string; tool: string; outcome: string; latency_ms: number; model: string; created_at: string }[];
};
export type Explanation = { status: string; explanation: string; tool_calls: number; cost_minor: number; model: string };
