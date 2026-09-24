"""An in-memory stand-in for the two Lua scripts in `app/queue.py`.

The fakes can't run Lua, so `eval` recognises each script by what it does and applies the same
semantics to a plain dict. That's enough for the callers' tests; the scripts themselves are
exercised against a real Redis by the e2e stack (and `test_queue_scripts.py` when one is
reachable).
"""


class ScriptedRedis:
    """Mixin: gives a fake with a `store` dict and `deleted` list an `eval`."""

    async def eval(self, script, numkeys, *keys_and_args):
        key, arg = keys_and_args[0], keys_and_args[numkeys]
        if "DEL" in script:                      # compare-and-delete
            if self.store.get(key) == arg:
                self.deleted.append(key)
                del self.store[key]
                return 1
            return 0
        count = int(self.store.get(key, 0)) + 1   # INCR with TTL
        self.store[key] = count
        self.ttls = getattr(self, "ttls", {})
        self.ttls.setdefault(key, int(arg))
        return count
