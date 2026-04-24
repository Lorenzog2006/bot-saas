-- Esegui questo script nel SQL Editor di Supabase

CREATE TABLE clients (
  id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
  name TEXT NOT NULL,
  telegram_token TEXT UNIQUE NOT NULL,
  owner_chat_id TEXT NOT NULL,
  owner_name TEXT DEFAULT 'il proprietario',
  active BOOLEAN DEFAULT true,
  notes TEXT DEFAULT '',
  created_at TEXT DEFAULT to_char(NOW(), 'DD/MM/YYYY HH24:MI')
);

CREATE TABLE apartment_content (
  client_id TEXT PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
  content TEXT DEFAULT '',
  updated_at TEXT DEFAULT to_char(NOW(), 'DD/MM/YYYY HH24:MI')
);

CREATE TABLE media_items (
  id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
  client_id TEXT REFERENCES clients(id) ON DELETE CASCADE,
  keywords TEXT NOT NULL,
  tipo TEXT DEFAULT 'photo',
  file_id TEXT NOT NULL,
  caption TEXT DEFAULT '',
  created_at TEXT DEFAULT to_char(NOW(), 'DD/MM/YYYY HH24:MI')
);

CREATE TABLE bookings (
  id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
  client_id TEXT REFERENCES clients(id) ON DELETE CASCADE,
  guest_chat_id TEXT NOT NULL,
  nome TEXT DEFAULT 'Ospite',
  checkin TEXT DEFAULT '',
  checkout TEXT DEFAULT '',
  lingua TEXT DEFAULT 'italian',
  created_at TEXT DEFAULT to_char(NOW(), 'DD/MM/YYYY HH24:MI'),
  UNIQUE(client_id, guest_chat_id)
);

CREATE TABLE daily_stats (
  client_id TEXT REFERENCES clients(id) ON DELETE CASCADE,
  stat_date TEXT NOT NULL,
  totale INTEGER DEFAULT 0,
  lingue TEXT DEFAULT '{}',
  argomenti TEXT DEFAULT '{}',
  ospiti TEXT DEFAULT '[]',
  PRIMARY KEY (client_id, stat_date)
);
