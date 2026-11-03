"""Community scope is a local product boundary, without remote licensing."""
MAX_STORES = 1
PRODUCT_NAME = '抖店智能客服社区版'
DATA_NAMESPACE = 'FeigeCustomerServiceCommunity'
BASE_CDP_PORT = 19422


def require_platform(platform):
    if platform != 'feige':
        raise ValueError('社区版仅支持抖店飞鸽')


def require_capacity(specs, adding=False):
    if len(specs) + int(adding) > MAX_STORES:
        raise ValueError('社区版最多接入 1 家抖店；停用店铺仍占用名额')
