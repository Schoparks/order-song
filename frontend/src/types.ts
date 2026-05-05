export type TrackSource = "qq" | "netease" | "kugou" | "bilibili";
export type QueueStatus = "queued" | "playing" | "played" | "removed";
export type RoomMode = "order_only" | "play_enabled";

export interface UserPublic {
  id: number;
  username: string;
  is_admin: boolean;
  created_at: string;
}

export interface LoginOut {
  token: string;
  user: UserPublic;
}

export interface Room {
  id: number;
  name: string;
  created_by_user_id: number;
  created_at: string;
  member_count?: number;
  member_names?: string[];
}

export interface Track {
  id?: number;
  source: TrackSource;
  source_track_id: string;
  title: string;
  artist?: string | null;
  duration_ms?: number | null;
  cover_url?: string | null;
  audio_url?: string | null;
  loudness_gain_db?: number | null;
  loudness_peak?: number | null;
  loudness_source?: string | null;
  loudness_error?: string | null;
  parts?: Track[];
}

export interface QueueItem {
  id: number;
  status: QueueStatus;
  created_at: string;
  ordered_by: Pick<UserPublic, "id" | "username">;
  track: Track;
}

export interface PlaybackState {
  room_id: number;
  mode: RoomMode;
  current_queue_item_id: number | null;
  is_playing: boolean;
  position_ms: number;
  volume: number;
  updated_at: string;
}

export interface PlaybackEnvelope {
  playback_state: PlaybackState;
  current_track?: Track | null;
  ordered_by?: Pick<UserPublic, "id" | "username"> | null;
  queue?: QueueItem[];
  server_time?: string;
  server_ts_ms?: number;
  effective_position_ms?: number;
}

export interface Playlist {
  id: number;
  name: string;
  created_at: string;
  item_count: number;
}

export interface PlaylistItem {
  id: number;
  created_at: string;
  track: Track;
}

export interface TrendingItem {
  track: Track;
  order_count: number;
  last_ordered_at: string;
}

export interface PublicConfig {
  trending?: {
    limit?: number;
  };
  client?: {
    sync_interval_ms?: number;
    trending_sync_interval_ms?: number;
    room_check_interval_ms?: number;
    rooms_refresh_interval_ms?: number;
    search_history_limit?: number;
  };
}

export type WsMessage =
  | ({ type: "playback_updated"; room_id: number } & PlaybackEnvelope)
  | { type: "queue_updated" }
  | { type: "room_destroyed"; room_id: number }
  | { type: "room_member_left"; room_id: number; user_id: number }
  | { type: "room_member_joined"; room_id: number; user_id: number }
  | { type: string; [key: string]: unknown };
