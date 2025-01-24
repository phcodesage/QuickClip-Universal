# Monkey patch pydub to work without audioop
import sys
import types

# Create a dummy audioop module
dummy_audioop = types.ModuleType('audioop')

# Add dummy functions that pydub might call
def dummy_func(*args, **kwargs):
    return None

# Add common audioop functions
for func_name in ['getsizeof', 'mul', 'ratecv', 'tostereo', 'tomono']:
    setattr(dummy_audioop, func_name, dummy_func)

# Insert the dummy module into sys.modules
sys.modules['audioop'] = dummy_audioop
sys.modules['pyaudioop'] = dummy_audioop 