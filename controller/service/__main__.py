import logging

from controller.service.main import main

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
raise SystemExit(main())
