-- Map a Docker container id (from the tail file path) to its service name by reading
-- the container's config.v2.json (mounted read-only). No docker socket. Cached per id.
local cache = {}

function set_service(tag, ts, record)
    local file = record["file"]
    if file == nil then
        record["service"] = "unknown"
        return 2, ts, record
    end
    local id = string.match(file, "containers/([0-9a-f]+)/")
    if id == nil then
        record["service"] = "unknown"
        return 2, ts, record
    end
    local name = cache[id]
    if name == nil then
        local fh = io.open("/var/lib/docker/containers/" .. id .. "/config.v2.json", "r")
        if fh ~= nil then
            local data = fh:read("*a")
            fh:close()
            -- config.v2.json stores "Name":"/rebate-bot-bot-1"
            name = string.match(data, '"Name":"/?([^"]+)"')
            if name ~= nil then cache[id] = name end
        end
    end
    record["service"] = name or "unknown"
    return 2, ts, record
end
