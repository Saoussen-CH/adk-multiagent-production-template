from customer_support_mas.providers.models import Order, Product


def test_order_defaults():
    order = Order(order_id="ORD-1", customer_id="user-1", status="Delivered")
    assert order.items == []
    assert order.timeline == []
    assert order.total is None


def test_order_with_items():
    order = Order(
        order_id="ORD-1",
        customer_id="user-1",
        status="Delivered",
        items=[{"product_id": "PROD-1", "name": "Widget", "price": 9.99, "qty": 2}],
    )
    assert order.items[0]["product_id"] == "PROD-1"


def test_product_construction():
    product = Product(product_id="PROD-1", name="Widget", price=9.99)
    assert product.keywords == []
