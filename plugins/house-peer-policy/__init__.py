"""Optional per-profile peer policy in the native always-composed prompt layer.

This is model-facing application policy, not a filesystem/tool sandbox. Pair
admission and destination restrictions remain native transport responsibilities.
"""

def register(ctx):
    policy = ctx.get_config('policy', '')
    if not policy:
        return
    if not isinstance(policy, str) or len(policy) > 2400:
        raise ValueError('Peer policy must be nonempty text of at most 2400 characters')
    ctx.register_system_prompt_section(
        'a2a.house-work-scope', policy, position='after_memory', max_chars=2400,
    )
