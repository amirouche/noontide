all:
	@rm -rf build || echo fresh start
	python noontide.py https://www.notion.so/hyper-dev-016dae933f5347b48cbe426ba52f362b
	cd build && ln -sf hyper-dev-016dae933f5347b48cbe426ba52f362b.html index.html
	cd build && ln -sf Ordered-Key-Value-Stores-1bfd6ef99cac41e0a8193a1c8738e06a.html ordered-key-value-stores.html
