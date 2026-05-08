import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

def test_enrichment_with_vendor_data():
    """Test local enrichment with real dat extracted from third-party vendor admin"""
    vendor_data = {
        "transaction_id": "a9b0d9ed-5624-4877-86aa-f27d42b784f1",
        "email": "blue_shay@hotmail.com",
        "phone_number": "+447492070776"
    }
    
    # We will test using FastAPI TestClient 
    from fastapi.testclient import TestClient
    client = TestClient(app)
    
    # Test Email Enrichment
    response = client.post("/api/v1/enrichment/email", json={
        "email": vendor_data["email"],
        "entity_id": "vendor_test_user_1"
    })
    assert response.status_code == 200
    email_data = response.json()
    assert email_data["email"] == vendor_data["email"]
    assert email_data["is_free_provider"] is True
    
    # Test Phone Enrichment
    response = client.post("/api/v1/enrichment/phone", json={
        "phone": vendor_data["phone_number"],
        "entity_id": "vendor_test_user_1"
    })
    assert response.status_code == 200
    phone_data = response.json()
    assert phone_data["phone"] == vendor_data["phone_number"]
    assert phone_data["valid_format"] is True

    # Test Full Transaction Enrichment
    req_body = {
        "transaction_id": vendor_data["transaction_id"],
        "sender_email": vendor_data["email"],
        "sender_phone": vendor_data["phone_number"]
    }
    response = client.post("/api/v1/enrichment/transaction", json=req_body)
    assert response.status_code == 200
    tx_data = response.json()
    assert tx_data["transaction_id"] == vendor_data["transaction_id"]
    assert "sender_email" in tx_data["enrichments"]
    assert "sender_phone" in tx_data["enrichments"]

    print("Success: Local enrichment offline implementation successfully tested with real third-party vendor data!")
