import React from 'react';
export default class ErrorBoundary extends React.Component<{children:React.ReactNode},{failed:boolean}>{
 state={failed:false};static getDerivedStateFromError(){return {failed:true}}
 render(){return this.state.failed?<main className="recovery-screen"><h1>Không hiển thị được màn hình này</h1><p>Dữ liệu đã lưu vẫn còn. Tải lại để quay về danh sách vị trí tuyển dụng.</p><button onClick={()=>window.location.reload()}>Tải lại ứng dụng</button></main>:this.props.children}
}
