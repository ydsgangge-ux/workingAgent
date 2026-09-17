import psutil, time

proc = psutil.Process(19432)
print(f'Name: {proc.name()}')
print(f'Status: {proc.status()}')
print(f'CPU: {proc.cpu_percent(interval=1)}%')
print(f'Memory: {proc.memory_info().rss / 1024 / 1024:.0f} MB')
print(f'Threads: {len(proc.threads())}')
ct = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(proc.create_time()))
print(f'Create time: {ct}')
cons = proc.connections()
print(f'Connections: {len(cons)}')
for c in cons[:8]:
    laddr = f'{c.laddr.ip}:{c.laddr.port}' if c.laddr else '-'
    raddr = f'{c.raddr.ip}:{c.raddr.port}' if c.raddr else '-'
    print(f'  {laddr} -> {raddr} ({c.status})')
open(r'd:\AGI-PRO-main\_proc_info.txt', 'w', encoding='utf-8').write(f'Process info dumped at {time.time()}')