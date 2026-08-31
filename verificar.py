import ast, sys
files = [
    'services/pdf_remision_service.py',
    'handlers/pdf_handler.py',
    'handlers/router.py',
]
ok = True
for f in files:
    try:
        ast.parse(open(f, encoding='utf-8').read())
        print('OK', f)
    except SyntaxError as e:
        print('ERR', f, e.lineno, e.msg)
        ok = False
sys.exit(0 if ok else 1)
