-- Redact secrets/full-addresses before egress; extract a coarse level. Mirrors the
-- STRENGTHENED Python reference in tests/test_log_redaction.py (kept in sync by hand;
-- see that file's docstring and ops/fluent-bit/README.md).
local SECRET_KEYS = {
    "private_key", "master_key", "api_secret", "passphrase",
    "bot_token", "secret", "seed", "authorization",
}

-- Optional auth-scheme prefix that, when present right after the separator, is
-- swallowed into the redaction along with the token that follows it. Mirrors the
-- `((?:bearer|basic|token|digest)\s+)?` group in _SECRET_KV (tests/test_log_redaction.py)
-- so "authorization: Bearer <token>" redacts the whole "Bearer <token>", not just the
-- word "Bearer".
local AUTH_SCHEMES = { "bearer", "basic", "token", "digest" }

local function redact_str(s)
    if type(s) ~= "string" then return s end
    -- 0x + hex run: redact ONLY when exactly 40 hex (EVM address). 64-hex txhash kept.
    -- A masked address uses an ellipsis so it is never a contiguous hex run and never
    -- matches here.
    s = s:gsub("0x%x+", function(tok)
        if #tok - 2 == 40 then return "0x<redacted>" end
        return tok
    end)

    -- key=value / key: value where key is a secret (case-insensitive via lower copy).
    -- `%f[%a]` is a word-start frontier so "notsecret=x" (kw glued to a longer word)
    -- does not match, approximating the Python `\b` before the keyword alternation.
    local lower = s:lower()
    for _, kw in ipairs(SECRET_KEYS) do
        local from = 1
        while true do
            local a, sepEnd = lower:find("%f[%a]" .. kw .. "%s*[=:]%s*", from)
            if a == nil then break end

            local valStart = sepEnd + 1

            -- Does the value start with an auth-scheme word + whitespace?
            local schemeEnd = nil
            for _, scheme in ipairs(AUTH_SCHEMES) do
                local _, e = lower:find("^" .. scheme .. "%s+", valStart)
                if e ~= nil then
                    schemeEnd = e
                    break
                end
            end

            local tokenStart = (schemeEnd ~= nil) and (schemeEnd + 1) or valStart
            local _, tokenEnd = lower:find("^%S+", tokenStart)

            if tokenEnd == nil then
                -- No value token at all (e.g. key at end of string) — nothing to
                -- redact for this occurrence; Python's \S+ requires >=1 char too.
                from = valStart
            else
                -- Keep "key" + separator exactly as originally written (case and
                -- whitespace preserved); drop the optional scheme word and the value
                -- token entirely, replacing them with a single "<redacted>" — no
                -- extra space inserted, matching group(1)+group(2)+"<redacted>" in
                -- the Python reference.
                local keep = s:sub(a, sepEnd)
                s = s:sub(1, a - 1) .. keep .. "<redacted>" .. s:sub(tokenEnd + 1)
                from = a + #keep + #"<redacted>"
                lower = s:lower()
            end
        end
    end
    -- JSON-quoted key/value form (production structlog output): "kw":"value" ->
    -- "kw":"<redacted>". Mirrors the Python reference's _SECRET_KV_JSON regex. Kept
    -- separate from the logfmt loop above so both forms are handled independently.
    for _, kw in ipairs(SECRET_KEYS) do
        s = s:gsub('"' .. kw .. '"%s*:%s*"[^"]*"', '"' .. kw .. '":"<redacted>"')
    end

    return s
end

local function level_of(s)
    if type(s) ~= "string" then return nil end
    local lvl = s:match('"level"%s*:%s*"(%a+)"')      -- JSON structlog
    if lvl ~= nil then return lvl:lower() end
    lvl = s:match("%[%s*(%a+)%s*%]")                   -- console fallback
    if lvl ~= nil then return lvl:lower() end
    return nil
end

function redact(tag, ts, record)
    local lvl = nil
    for k, v in pairs(record) do
        if lvl == nil then lvl = level_of(v) end
        record[k] = redact_str(v)
    end
    record["level"] = lvl or "unknown"
    return 2, ts, record
end
