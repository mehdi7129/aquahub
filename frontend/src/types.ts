export type Theme = 'light' | 'dark';
export type Page = 'overview' | 'updates' | 'drones' | 'history' | 'settings';
export interface Service { id: string; name: string; state: string; health: string | null; image_id?: string; restarts?: number; started_at?: string; actions?: { restart?: boolean; reason?: string; disabled_reason?: string }; }
export interface JobProgress { label?: string; completed?: number; total?: number; percent?: number; indeterminate?: boolean; phase?: string; }
export interface ManualCheck { id: string; label: string; details: string; }
export interface Job { id: string; status: string; label?: string; kind?: string; target?: string; created_at?: string; started_at?: string; finished_at?: string; message?: string; progress?: number | JobProgress; steps?: Array<{ label?: string; name?: string; status?: string; message?: string }>; error?: string | { message?: string }; result?: unknown; }
export interface HubStatus { version: string; machine: string; docker_available: boolean; services: Service[]; active_job: Job | null; jobs?: Job[]; bundle_path?: string | null; portainer_available: boolean; portainer?: { available?: boolean; restart_supported?: boolean; restart_reason?: string }; stale?: boolean; error?: { code?: string; message?: string } | null; collected_at?: string; }
export interface HubEvent { id: string | number; timestamp: string; service?: string | null; kind: string; message: string; log_snapshot?: { id: string | number; service?: string; reason?: string; status: string; captured_at?: string; bytes?: number; truncated?: boolean; message?: string; error?: string | { message?: string } }; }
export interface Drone { number: number; name: string; address: string; reachable: boolean | null; identity_verified: boolean; configured: boolean; supported: boolean; manual_checklist?: ManualCheck[]; }
export interface Check { label?: string; name?: string; ok?: boolean; status?: string; message?: string; }
export interface Preflight { id?: string; preflight_id?: string; allowed?: boolean; ok?: boolean; checks?: Check[]; message?: string; number?: number; address?: string; identity?: { machine_id: string; host_key_fingerprint: string }; needs_identity_confirmation?: boolean; manual_checklist?: ManualCheck[]; }
export interface Inspection { id?: string; inspection_id?: string; allowed?: boolean; compatible?: boolean; can_apply?: boolean; version?: string; checks?: Check[]; message?: string; path?: string; }
export interface Settings { github_url?: string; theme?: Theme; retention_days?: number; }
export interface SshSettings { known_hosts_file: string; identity_file: string; allow_none_auth: boolean; }
export interface Release { release_id: number; version: string; name: string; notes: string; assets: Array<{ asset_id: number; name: string; size: number; compatible: boolean; downloadable: boolean; sha256?: string; message?: string }>; compatible: boolean; }
export interface EventsPage { events: HubEvent[]; next_before_id?: number | string | null; has_more?: boolean; retention?: { days: number; max_events: number }; }
export interface Bridge { request(method: string, params?: Record<string, unknown>): Promise<unknown>; }
declare global { interface Window { aquahub?: Bridge; } }

export interface AppRelease { release_id: number; asset_id: number; version: string; tag: string; notes?: string; published_at?: string; asset_name: string; asset_size: number; sha256?: string; }
export interface AppUpdateStatus { current_version: string; repository: string; available: boolean; release?: AppRelease | null; download?: { version: string; ready: boolean; [key: string]: unknown } | null; installation?: { status: 'scheduled' | 'installing' | 'installed' | 'failed'; version: string; message?: string } | null; message?: string; }
